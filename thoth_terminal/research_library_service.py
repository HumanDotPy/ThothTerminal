from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, literal, or_, String
from sqlalchemy.orm import Session, defer, joinedload

from thoth_terminal.agent_actions import serialize_agent_action, upsert_agent_action
from thoth_terminal.config import settings
from thoth_terminal.db import utcnow
from thoth_terminal.ingest import ingest_source, upsert_internal_document
from thoth_terminal.models import Document, DocumentEvent, IngestionRun, Source, SourceState


RESEARCH_LIBRARY_SOURCE_NAME = "local_deep_research_library"


def _session_dialect_name(session: Session) -> str:
    bind = session.get_bind()
    return str(getattr(getattr(bind, "dialect", None), "name", "") or "").lower()


def _extra_json_value(dialect_name: str, key: str):
    if dialect_name == "postgresql":
        return Document.extra.op("->>")(key)
    return func.json_extract(Document.extra, f"$.{key}")


def _pg_choice(values):
    """Inline constant sets so the compiled predicate can match a partial index."""
    return [literal(value, type_=String, literal_execute=True) for value in values]


def _research_library_doc_filter(dialect_name: str):
    if dialect_name == "postgresql":
        # Literal keys and value sets keep the predicate matchable by
        # ix_documents_research_library; parameters would defeat the planner.
        research_library_value = Document.extra.op("->>")(
            literal("research_library", type_=String, literal_execute=True)
        )
        parent_value = Document.extra.op("->>")(
            literal("parent_research_document_id", type_=String, literal_execute=True)
        )
        return or_(
            Document.kind.in_(_pg_choice(["research_report", "research_addendum"])),
            research_library_value.in_(_pg_choice(["true", "1", "yes"])),
            parent_value.isnot(None),
        )
    research_library_value = _extra_json_value(dialect_name, "research_library")
    parent_value = _extra_json_value(dialect_name, "parent_research_document_id")
    return or_(
        Document.kind.in_(["research_report", "research_addendum"]),
        research_library_value == True,  # noqa: E712
        parent_value.isnot(None),
    )


def _dt_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _is_library_doc(doc: Document) -> bool:
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    return bool(extra.get("research_library") or extra.get("parent_research_document_id"))


def _query_terms(text: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", str(text or "")):
        term = raw.strip()
        if len(term) < 2:
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) >= 8:
            break
    return terms


def _library_relevance(doc: Document, summary_text: str, query: str) -> int:
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    topics = " ".join(str(row) for row in (extra.get("topics") or extra.get("subject_hints") or []) if str(row).strip())
    title_surface = str(doc.title or "").lower()
    surface = " ".join(
        str(part or "")
        for part in (
            doc.title,
            summary_text,
            doc.url,
            doc.canonical_url,
            doc.kind,
            extra.get("source_tier"),
            topics,
        )
    ).lower()
    needle = " ".join(str(query or "").lower().split())
    score = 0
    if needle and needle in surface:
        score += 40
    for term in _query_terms(query):
        lowered = term.lower()
        if lowered in surface:
            score += 5
        if lowered in title_surface:
            score += 4
    if doc.kind in {"research_source_card", "technical_source_card", "conference_paper_card"}:
        score += 12
    if extra.get("source_pack_seed"):
        score += 8
    if extra.get("research_library"):
        score += 4
    return score


def _lifecycle(doc: Document) -> dict[str, Any]:
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    raw = extra.get("report_lifecycle") if isinstance(extra.get("report_lifecycle"), dict) else {}
    return {
        "full_refresh_days": int(raw.get("full_refresh_days") or getattr(settings, "research_report_full_refresh_days", 90) or 90),
        "addendum_extension_days": int(raw.get("addendum_extension_days") or getattr(settings, "research_report_addendum_extension_days", 45) or 45),
        "update_policy": str(raw.get("update_policy") or "quarterly_full_refresh_with_addenda_extensions"),
        "memory_role": str(raw.get("memory_role") or "compiled_research"),
    }


def _parse_iso_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _normalize_conference_metadata(conference: object, conference_year: object = None) -> tuple[str | None, int | None]:
    text = str(conference or "").strip()
    year: int | None = None
    if conference_year is not None and str(conference_year).strip():
        try:
            year = int(str(conference_year).strip())
        except (TypeError, ValueError):
            year = None
    match = re.search(r"(?:\s|^)(20\d{2})$", text)
    if match:
        if year is None:
            year = int(match.group(1))
        text = text[: match.start()].strip()
    return (text or None), year


def _next_review_at(doc: Document) -> datetime | None:
    lifecycle = _lifecycle(doc)
    base = doc.published_at or doc.ingested_at
    if base is None:
        return None
    due = base + timedelta(days=max(1, int(lifecycle["full_refresh_days"])))
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    last_addendum = _parse_iso_datetime(extra.get("last_addendum_at"))
    if last_addendum is not None:
        extended = last_addendum + timedelta(days=max(0, int(lifecycle["addendum_extension_days"])))
        due = max(due, extended)
    return due


def serialize_research_library_doc(
    doc: Document,
    *,
    include_text: bool = False,
    summary_text: str | None = None,
) -> dict[str, Any]:
    extra = doc.extra if isinstance(doc.extra, dict) else {}
    conference, conference_year = _normalize_conference_metadata(extra.get("conference"), extra.get("conference_year"))
    lifecycle = _lifecycle(doc)
    next_review = _next_review_at(doc)
    stale = bool(next_review and next_review <= utcnow())
    text = (doc.translation_en or doc.normalized_text or "") if include_text or summary_text is None else ""
    summary = summary_text if summary_text is not None else text[:700]
    return {
        "id": int(doc.id),
        "kind": doc.kind,
        "title": doc.title,
        "author": doc.author,
        "source": doc.source.name if doc.source else None,
        "url": doc.url or doc.canonical_url,
        "published_at": _dt_iso(doc.published_at),
        "ingested_at": _dt_iso(doc.ingested_at),
        "updated_at": _dt_iso(doc.triage_updated_at),
        "source_path": extra.get("source_path"),
        "file_name": extra.get("file_name"),
        "content_kind": extra.get("content_kind") or doc.kind,
        "source_tier": extra.get("source_tier"),
        "source_url": extra.get("source_url") or doc.url or doc.canonical_url,
        "conference": conference,
        "conference_year": conference_year,
        "topics": list(extra.get("topics") or []),
        "source_pack_seed": bool(extra.get("source_pack_seed")),
        "parent_research_document_id": extra.get("parent_research_document_id"),
        "subject_hints": list(extra.get("subject_hints") or []),
        "lifecycle": lifecycle,
        "last_addendum_at": extra.get("last_addendum_at"),
        "addendum_count": len(extra.get("addenda") or []),
        "next_full_review_at": _dt_iso(next_review),
        "stale": stale,
        "summary": summary[:700],
        "text": text if include_text else None,
        "href": f"/docs/{int(doc.id)}",
    }


def ensure_research_library_source(session: Session, *, library_dir: str | None = None) -> Source:
    now = utcnow()
    cfg = {
        "connector": "local_research_library",
        "library_dir": library_dir or str(getattr(settings, "research_library_inbox_dir", "research/inbox")),
        "max_files_per_run": int(getattr(settings, "research_library_max_files_per_run", 50) or 50),
        "full_refresh_days": int(getattr(settings, "research_report_full_refresh_days", 90) or 90),
        "addendum_extension_days": int(getattr(settings, "research_report_addendum_extension_days", 45) or 45),
        "refresh_existing": True,
        "enabled": True,
        "default_domains": [],
        "default_tags": ["deep-research", "research-library"],
        "default_event_types": ["research_report"],
        "source_quality": {
            "authority": "internal_compiled",
            "timeliness": "manual_dropbox",
            "acquisition_fragility": "low",
            "rights_class": "user_provided",
        },
    }
    src = session.query(Source).filter_by(name=RESEARCH_LIBRARY_SOURCE_NAME).first()
    if src is None:
        src = Source(
            name=RESEARCH_LIBRARY_SOURCE_NAME,
            category="research",
            priority=3,
            polling_interval_seconds=3600,
            config=cfg,
            created_at=now,
        )
        session.add(src)
        session.flush()
    else:
        existing = dict(src.config or {})
        existing.update({key: value for key, value in cfg.items() if key not in existing})
        for key in {
            "connector",
            "enabled",
            "refresh_existing",
            "library_dir",
            "max_files_per_run",
            "full_refresh_days",
            "addendum_extension_days",
        }:
            if key in cfg:
                existing[key] = cfg[key]
        if library_dir:
            existing["library_dir"] = library_dir
        src.category = "research"
        src.priority = max(int(src.priority or 1), 3)
        src.polling_interval_seconds = src.polling_interval_seconds or 3600
        src.config = existing
        session.flush()
    if session.query(SourceState).filter_by(source_id=int(src.id)).first() is None:
        session.add(SourceState(source_id=int(src.id), cursor={}, updated_at=now))
        session.flush()
    return src


def ingest_research_library(session: Session, *, library_dir: str | None = None) -> dict[str, Any]:
    source = ensure_research_library_source(session, library_dir=library_dir)
    result = ingest_source(session, source)
    run = (
        session.query(IngestionRun)
        .filter_by(source_id=int(source.id))
        .order_by(IngestionRun.id.desc())
        .first()
    )
    return {
        "source_id": int(source.id),
        "source_name": source.name,
        "seen": result.seen,
        "new": result.new,
        "updated": result.updated,
        "run_id": int(run.id) if run else None,
        "run_status": run.status if run else None,
        "run_error": run.error if run else None,
        "library_dir": (source.config or {}).get("library_dir"),
    }


def list_research_library(
    session: Session,
    *,
    query: str | None = None,
    conference: str | None = None,
    conference_year: int | None = None,
    content_kind: str | None = None,
    stale_only: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    summary_expr = func.substr(func.coalesce(Document.translation_en, Document.normalized_text, ""), 1, 700).label("summary_text")
    q = session.query(Document, summary_expr).options(
        joinedload(Document.source),
        defer(Document.raw_text),
        defer(Document.normalized_text),
        defer(Document.translation_en),
    )
    q = q.filter(_research_library_doc_filter(_session_dialect_name(session)))
    dialect_name = _session_dialect_name(session)
    conf_text = str(conference or "").strip()
    if conf_text:
        conf_value = _extra_json_value(dialect_name, "conference")
        q = q.filter(conf_value.ilike(f"%{conf_text}%"))
    if conference_year is not None:
        year_value = _extra_json_value(dialect_name, "conference_year")
        q = q.filter(year_value == str(int(conference_year)) if dialect_name == "postgresql" else year_value == int(conference_year))
    kind_text = str(content_kind or "").strip()
    if kind_text:
        kind_value = _extra_json_value(dialect_name, "content_kind")
        q = q.filter(or_(Document.kind == kind_text, kind_value == kind_text))
    text = str(query or "").strip()
    if text:
        like = f"%{text}%"
        exact_match = or_(Document.title.ilike(like), Document.normalized_text.ilike(like))
        term_filters = []
        for term in _query_terms(text):
            term_like = f"%{term}%"
            term_filters.append(or_(Document.title.ilike(term_like), Document.normalized_text.ilike(term_like)))
        q = q.filter(or_(exact_match, and_(*term_filters), or_(*term_filters)) if term_filters else exact_match)
    limit_val = max(1, min(int(limit or 50), 200))
    fetch_limit = min(200, max(limit_val, limit_val * 5 if text else limit_val))
    rows = q.order_by(Document.ingested_at.desc(), Document.id.desc()).limit(fetch_limit).all()
    if text:
        rows = sorted(rows, key=lambda row: (-_library_relevance(row[0], str(row[1] or ""), text), str(row[0].ingested_at or ""), int(row[0].id or 0)))
    serialized = [
        serialize_research_library_doc(doc, summary_text=str(summary_text or ""))
        for doc, summary_text in rows[:limit_val]
        if _is_library_doc(doc) or doc.kind in {"research_report", "research_addendum"}
    ]
    if stale_only:
        serialized = [row for row in serialized if row.get("stale")]
    return {
        "count": len(serialized),
        "stale_count": sum(1 for row in serialized if row.get("stale")),
        "reports": serialized,
    }


def list_research_conference_index(
    session: Session,
    *,
    query: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Return a grouped index of conference/year source-card coverage."""

    summary_expr = func.substr(func.coalesce(Document.translation_en, Document.normalized_text, ""), 1, 700).label("summary_text")
    q = session.query(Document, summary_expr).options(
        joinedload(Document.source),
        defer(Document.raw_text),
        defer(Document.normalized_text),
        defer(Document.translation_en),
    )
    dialect_name = _session_dialect_name(session)
    q = q.filter(_research_library_doc_filter(dialect_name))
    conf_value = _extra_json_value(dialect_name, "conference")
    q = q.filter(conf_value.isnot(None))
    text = str(query or "").strip()
    if text:
        like = f"%{text}%"
        exact_match = or_(Document.title.ilike(like), Document.normalized_text.ilike(like))
        term_filters = []
        for term in _query_terms(text):
            term_like = f"%{term}%"
            term_filters.append(or_(Document.title.ilike(term_like), Document.normalized_text.ilike(term_like)))
        q = q.filter(or_(exact_match, and_(*term_filters), or_(*term_filters)) if term_filters else exact_match)
    rows = q.order_by(Document.ingested_at.desc(), Document.id.desc()).limit(1000).all()
    if text:
        normalized_query = " ".join(text.lower().split())
        exact_conference_rows = []
        for doc, summary_text in rows:
            extra = doc.extra if isinstance(doc.extra, dict) else {}
            conference, year = _normalize_conference_metadata(extra.get("conference"), extra.get("conference_year"))
            conference_text = str(conference or "").strip().lower()
            conference_year_text = f"{conference_text} {int(year)}" if year is not None and conference_text else ""
            if normalized_query in {conference_text, conference_year_text}:
                exact_conference_rows.append((doc, summary_text))
        if exact_conference_rows:
            rows = exact_conference_rows
        rows = sorted(rows, key=lambda row: (-_library_relevance(row[0], str(row[1] or ""), text), str(row[0].ingested_at or ""), int(row[0].id or 0)))

    grouped: dict[tuple[str, int | None], dict[str, Any]] = {}
    for doc, summary_text in rows:
        extra = doc.extra if isinstance(doc.extra, dict) else {}
        conference, year = _normalize_conference_metadata(extra.get("conference"), extra.get("conference_year"))
        if not conference:
            continue
        key = (conference, year)
        row = grouped.setdefault(
            key,
            {
                "conference": conference,
                "conference_year": year,
                "count": 0,
                "content_kind_counts": {},
                "source_tier_counts": {},
                "topics": {},
                "sample_items": [],
            },
        )
        row["count"] += 1
        content_kind = str(extra.get("content_kind") or doc.kind or "document")
        source_tier = str(extra.get("source_tier") or "unknown")
        row["content_kind_counts"][content_kind] = int(row["content_kind_counts"].get(content_kind) or 0) + 1
        row["source_tier_counts"][source_tier] = int(row["source_tier_counts"].get(source_tier) or 0) + 1
        for topic in extra.get("topics") or extra.get("subject_hints") or []:
            topic_text = str(topic or "").strip()
            if not topic_text:
                continue
            row["topics"][topic_text] = int(row["topics"].get(topic_text) or 0) + 1
        if len(row["sample_items"]) < 6:
            row["sample_items"].append(
                {
                    "id": int(doc.id),
                    "title": doc.title,
                    "content_kind": content_kind,
                    "source_tier": source_tier,
                    "source_url": extra.get("source_url") or doc.url or doc.canonical_url,
                    "summary": str(summary_text or "")[:300],
                }
            )

    conferences: list[dict[str, Any]] = []
    for row in grouped.values():
        topics = sorted(row.pop("topics").items(), key=lambda item: (-int(item[1]), str(item[0]).lower()))
        row["top_topics"] = [{"topic": topic, "count": int(count)} for topic, count in topics[:16]]
        conferences.append(row)
    conferences = sorted(
        conferences,
        key=lambda row: (
            str(row.get("conference") or "").lower(),
            -(int(row["conference_year"]) if row.get("conference_year") is not None else 0),
        ),
    )[: max(1, min(200, int(limit or 50)))]
    return {
        "count": len(conferences),
        "query": text or None,
        "conferences": conferences,
    }


def review_research_library(
    session: Session,
    *,
    guild_id: int | None = None,
    stale_only: bool = False,
    review_window_days: int = 14,
    limit: int = 50,
) -> dict[str, Any]:
    """Create actionable refresh tasks for compiled reports that need review."""

    now = utcnow()
    horizon = now + timedelta(days=max(0, int(review_window_days or 0)))
    library = list_research_library(session, stale_only=False, limit=max(1, min(int(limit or 50), 200)))
    candidates: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []

    for row in library.get("reports") or []:
        if not isinstance(row, dict):
            continue
        if row.get("kind") != "research_report" or row.get("parent_research_document_id"):
            continue
        next_review = _parse_iso_datetime(row.get("next_full_review_at"))
        is_stale = bool(row.get("stale"))
        due_soon = bool(next_review and next_review <= horizon)
        if stale_only and not is_stale:
            continue
        if not stale_only and not (is_stale or due_soon):
            continue

        reason = "stale" if is_stale else "due_soon"
        doc_id = int(row["id"])
        title = str(row.get("title") or f"research report {doc_id}").strip()
        candidates.append(
            {
                "document_id": doc_id,
                "title": title,
                "review_reason": reason,
                "next_full_review_at": row.get("next_full_review_at"),
                "stale": is_stale,
            }
        )
        action = upsert_agent_action(
            session,
            guild_id=int(guild_id) if guild_id is not None else None,
            kind="research_report_refresh",
            title=f"Refresh compiled research report: {title}",
            priority=3 if is_stale else 5,
            dedupe_key=f"research_report_refresh:{doc_id}:{row.get('next_full_review_at') or 'undated'}",
            rationale=(
                "Compiled report memory is stale or approaching review. Search newer evidence, "
                "update affected Thoth objects, add an addendum for partial updates, or request a full report refresh."
            ),
            document_id=doc_id,
            due_at=next_review,
            payload={
                "source": "research_library_review",
                "review_reason": reason,
                "report_title": title,
                "source_path": row.get("source_path"),
                "subject_hints": row.get("subject_hints") or [],
                "next_full_review_at": row.get("next_full_review_at"),
                "expected_workflow": [
                    "list_research_library",
                    "get_research_library_report",
                    "object_search",
                    "add_research_report_addendum",
                    "create/update affected theses, notes, datasets, or supply chains only when warranted",
                ],
            },
        )
        actions.append(serialize_agent_action(action))

    return {
        "checked_count": int(library.get("count") or 0),
        "candidate_count": len(candidates),
        "action_count": len(actions),
        "review_window_days": int(review_window_days or 0),
        "stale_only": bool(stale_only),
        "candidates": candidates,
        "actions": actions,
    }


def get_research_library_report(session: Session, *, document_id: int) -> dict[str, Any]:
    doc = session.query(Document).options(joinedload(Document.source)).filter_by(id=int(document_id)).first()
    if doc is None or not (_is_library_doc(doc) or doc.kind in {"research_report", "research_addendum"}):
        raise ValueError("Research library document not found.")
    return serialize_research_library_doc(doc, include_text=True)


def upsert_research_source_card(
    session: Session,
    *,
    title: str,
    body_md: str,
    url: str | None = None,
    source_tier: str = "technical paper/conference material",
    content_kind: str = "research_source_card",
    conference: str | None = None,
    conference_year: int | None = None,
    topics: list[str] | None = None,
    published_at: datetime | None = None,
    external_id: str | None = None,
    actor: str = "automation",
) -> dict[str, Any]:
    """Create a durable research-library source card for papers, decks, and technical sources."""

    title_v = str(title or "").strip()
    body_v = str(body_md or "").strip()
    if not title_v:
        raise ValueError("title is required.")
    if not body_v:
        raise ValueError("body_md is required.")
    url_v = str(url or "").strip() or None
    conference_v, conference_year_v = _normalize_conference_metadata(conference, conference_year)
    topic_rows = [str(row).strip() for row in (topics or []) if str(row).strip()]
    explicit_external_id = str(external_id or "").strip() or None
    stable = explicit_external_id or f"research-source-card:{hashlib.sha256((url_v or title_v).encode('utf-8')).hexdigest()[:20]}"
    canonical_url_v = url_v
    if explicit_external_id and url_v:
        fragment = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:12]
        canonical_url_v = f"{url_v}#thoth-card-{fragment}"
    text = "\n".join(
        part
        for part in [
            f"Source tier: {source_tier}",
            f"URL: {url_v}" if url_v else "",
            f"Conference: {conference_v}" if conference_v else "",
            f"Conference year: {conference_year_v}" if conference_year_v else "",
            f"Topics: {', '.join(topic_rows)}" if topic_rows else "",
            "",
            body_v,
        ]
        if part != ""
    )
    doc = upsert_internal_document(
        session,
        external_id=stable,
        title=title_v,
        text=text,
        kind=str(content_kind or "research_source_card").strip() or "research_source_card",
        url=url_v,
        canonical_url=canonical_url_v,
        published_at=published_at,
        extra={
            "research_library": True,
            "source_pack_seed": True,
            "content_kind": str(content_kind or "research_source_card").strip() or "research_source_card",
            "source_tier": str(source_tier or "technical paper/conference material").strip(),
            "source_url": url_v,
            "conference": conference_v,
            "conference_year": conference_year_v,
            "topics": topic_rows,
            "subject_hints": topic_rows,
            "actor": str(actor or "automation"),
            "report_lifecycle": {
                "memory_role": "source_evidence",
                "update_policy": "source_card_refresh_when_claim_changes",
                "full_refresh_days": 365,
                "addendum_extension_days": 0,
            },
        },
    )
    return serialize_research_library_doc(doc, include_text=True)


def add_research_report_addendum(
    session: Session,
    *,
    document_id: int,
    title: str,
    body_md: str,
    actor: str = "automation",
) -> dict[str, Any]:
    parent = session.query(Document).filter_by(id=int(document_id)).first()
    if parent is None or not (_is_library_doc(parent) or parent.kind == "research_report"):
        raise ValueError("Research library parent document not found.")
    now = utcnow()
    addendum_title = str(title or "").strip() or f"Addendum to {parent.title or parent.id}"
    body = str(body_md or "").strip()
    if not body:
        raise ValueError("body_md is required.")
    digest = hashlib.sha256(f"{int(parent.id)}\n{addendum_title}\n{body}".encode("utf-8")).hexdigest()[:16]
    external_id = f"research-addendum:{int(parent.id)}:{digest}"
    addendum = upsert_internal_document(
        session,
        external_id=external_id,
        title=addendum_title,
        text=body,
        kind="research_addendum",
        canonical_url=parent.canonical_url,
        url=parent.url,
        published_at=now,
        extra={
            "research_library": True,
            "parent_research_document_id": int(parent.id),
            "content_kind": "research_addendum",
            "actor": str(actor or "automation"),
        },
    )
    extra = dict(parent.extra or {})
    addenda = [dict(row) for row in (extra.get("addenda") or []) if isinstance(row, dict)]
    addenda.append(
        {
            "document_id": int(addendum.id),
            "title": addendum.title,
            "actor": str(actor or "automation"),
            "created_at": now.isoformat(),
        }
    )
    extra["addenda"] = addenda[-25:]
    extra["last_addendum_at"] = now.isoformat()
    extra["report_update_status"] = "extended_by_addendum"
    parent.extra = extra
    session.add(
        DocumentEvent(
            document_id=int(parent.id),
            event_type="RESEARCH_ADDENDUM",
            event_at=now,
            meta={"addendum_document_id": int(addendum.id), "actor": str(actor or "automation")},
        )
    )
    session.flush()
    return {
        "parent": serialize_research_library_doc(parent),
        "addendum": serialize_research_library_doc(addendum, include_text=True),
    }
