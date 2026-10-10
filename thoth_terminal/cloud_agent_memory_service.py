from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Iterable, NamedTuple

from sqlalchemy import case, or_
from sqlalchemy.orm import Session

from thoth_terminal.models import CloudAgentMemoryEvent, CloudAgentMemorySegment
from thoth_terminal.research_state_tokens import POLICIES, budget_status, count_tokens, token_page


CLOUD_RESEARCH_HEARTBEAT_AGENT_KEY = "cloud_research_heartbeat"

SEGMENT_TYPES = {
    "identity",
    "operating_principle",
    "world_model",
    "working_model",
    "sector_model",
    "company_model",
    "mechanism",
    "contradiction",
    "source_model",
    "open_question",
    "episodic",
    "scratch",
}
SEGMENT_STATUSES = {"active", "archived", "superseded"}
CACHE_SCOPES = {"core", "stable", "active", "episodic", "scratch"}

_MEMORY_SCOPE_ORDER = {"core": 0, "stable": 1, "active": 2, "episodic": 3, "scratch": 4}
_MEMORY_SEARCH_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from", "had",
    "has", "have", "how", "in", "into", "is", "it", "its", "of", "on", "or", "our",
    "that", "the", "their", "this", "to", "was", "were", "what", "when", "where",
    "which", "who", "why", "will", "with", "would",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def estimate_tokens(text: object) -> int:
    return count_tokens(str(text or ""))


def _clean_text(value: object | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _clean_markdown(value: object | None) -> str:
    return str(value or "").strip()


def _slugify(value: object, *, default: str = "memory") -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return cleaned or default


def _normalize_agent_key(value: object | None) -> str:
    raw = str(value or "").strip()
    return raw[:128] if raw else CLOUD_RESEARCH_HEARTBEAT_AGENT_KEY


def _normalize_segment_key(value: object | None, *, title: str, segment_type: str) -> str:
    raw = str(value or "").strip()
    if raw:
        return raw[:255]
    return f"{segment_type}:{_slugify(title, default='untitled')}"[:255]


def _normalize_segment_type(value: object | None) -> str:
    raw = _slugify(value or "working_model").replace("-", "_")
    aliases = {
        "core": "operating_principle",
        "working": "working_model",
        "sector": "sector_model",
        "company": "company_model",
        "causal_model": "mechanism",
        "causal": "mechanism",
        "source": "source_model",
        "question": "open_question",
    }
    raw = aliases.get(raw, raw)
    return raw if raw in SEGMENT_TYPES else "working_model"


def _normalize_cache_scope(value: object | None, *, segment_type: str) -> str:
    raw = _slugify(value or "").replace("-", "_")
    if raw in CACHE_SCOPES:
        return raw
    if segment_type in {"identity", "operating_principle"}:
        return "core"
    if segment_type in {"world_model", "sector_model", "mechanism", "source_model"}:
        return "stable"
    if segment_type in {"episodic"}:
        return "episodic"
    if segment_type == "scratch":
        return "scratch"
    return "active"


def _normalize_status(value: object | None) -> str:
    raw = _slugify(value or "active").replace("-", "_")
    return raw if raw in SEGMENT_STATUSES else "active"


def _list_text(value: object) -> list[str]:
    if value is None:
        return []
    raw_items: list[object]
    if isinstance(value, str):
        raw_items = [part for part in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = _clean_text(item)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def _metadata(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def serialize_cloud_agent_memory_segment(
    row: CloudAgentMemorySegment, *, include_content: bool = True, match: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": int(row.id),
        "agent_key": row.agent_key,
        "segment_key": row.segment_key,
        "segment_type": row.segment_type,
        "cache_scope": row.cache_scope,
        "status": row.status,
        "title": row.title,
        "summary": row.summary,
        "priority": int(row.priority or 50),
        "importance": int(row.importance or 3),
        "token_estimate": int(row.token_estimate or 0),
        "entity_refs": row.entity_refs_json or [],
        "theme_refs": row.theme_refs_json or [],
        "metadata": row.metadata_json or {},
        "source_run_id": int(row.source_run_id) if row.source_run_id is not None else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "compressed_at": row.compressed_at.isoformat() if row.compressed_at else None,
    }
    if match is not None:
        payload["matched_terms"] = list(match.get("matched_terms") or [])
        payload["match_fields"] = list(match.get("match_fields") or [])
        payload["relevance_score"] = int(match.get("relevance_score") or 0)
    if include_content:
        payload["content_md"] = row.content_md or ""
    return payload


def serialize_cloud_agent_memory_event(row: CloudAgentMemoryEvent) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "agent_key": row.agent_key,
        "segment_id": int(row.segment_id) if row.segment_id is not None else None,
        "source_run_id": int(row.source_run_id) if row.source_run_id is not None else None,
        "event_kind": row.event_kind,
        "summary": row.summary,
        "content_md": row.content_md,
        "payload": deepcopy(row.payload_json or {}),
        "token_estimate": int(row.token_estimate or 0),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def render_memory_projections(session: Session, rows: list[CloudAgentMemorySegment]) -> dict[int, dict[str, Any]]:
    """Use exact-current summaries; protected and other-agent rows remain paged originals."""
    from thoth_terminal.research_state_compaction_service import render_compacted_states

    eligible = [row for row in rows if row.agent_key == CLOUD_RESEARCH_HEARTBEAT_AGENT_KEY
                and row.status == "active" and row.segment_type not in {"identity", "operating_principle"}]
    projections = render_compacted_states(session, "belief", eligible,
        source_contents={int(row.id): row.content_md or "" for row in eligible}) if eligible else {}
    for row in rows:
        if (row.agent_key == CLOUD_RESEARCH_HEARTBEAT_AGENT_KEY and row.status == "active"
                and row.segment_type not in {"identity", "operating_principle"}):
            view = projections[int(row.id)]
        else:
            page = token_page(row.content_md or "", limit_tokens=POLICIES["belief"].ceiling_tokens)
            view = {"content_md": page["text"], "source_page": page, "truncated": page["truncated"],
                    "compacted": False, "budget": budget_status("belief", row.content_md or ""),
                    "mode": "protected" if row.segment_type in {"identity", "operating_principle"} else "page"}
        view["source_locator"] = {"kind": "belief", "object_id": int(row.id), "content_field": "content_md"}
        if view.get("source_page"):
            view["source_page"] = {name: value for name, value in view["source_page"].items() if name != "text"}
        view["content_truncated"] = bool(view.get("truncated"))
        projections[int(row.id)] = view
    return projections


def _memory_event(
    session: Session,
    *,
    agent_key: str,
    event_kind: str,
    segment_id: int | None = None,
    source_run_id: int | None = None,
    summary: str | None = None,
    content_md: str | None = None,
    payload: dict[str, Any] | None = None,
) -> CloudAgentMemoryEvent:
    row = CloudAgentMemoryEvent(
        agent_key=agent_key,
        segment_id=segment_id,
        source_run_id=source_run_id,
        event_kind=_slugify(event_kind or "event").replace("-", "_")[:64],
        summary=_clean_text(summary) or None,
        content_md=_clean_markdown(content_md) or None,
        payload_json=deepcopy(payload or {}),
        token_estimate=estimate_tokens(content_md or summary or payload or ""),
        created_at=utcnow(),
    )
    session.add(row)
    return row


def upsert_cloud_agent_memory_segment(
    session: Session,
    *,
    agent_key: str | None = None,
    segment_key: str | None = None,
    segment_type: str = "working_model",
    title: str,
    content_md: str,
    cache_scope: str | None = None,
    summary: str | None = None,
    priority: int = 50,
    importance: int = 3,
    status: str = "active",
    entity_refs: object = None,
    theme_refs: object = None,
    metadata: object = None,
    source_run_id: int | None = None,
    change_summary: str | None = None,
) -> tuple[CloudAgentMemorySegment, bool]:
    agent_key_v = _normalize_agent_key(agent_key)
    segment_type_v = _normalize_segment_type(segment_type)
    title_v = _clean_text(title) or "Untitled memory"
    key_v = _normalize_segment_key(segment_key, title=title_v, segment_type=segment_type_v)
    content_v = _clean_markdown(content_md)
    cache_scope_v = _normalize_cache_scope(cache_scope, segment_type=segment_type_v)
    now = utcnow()
    values = {
        "segment_type": segment_type_v,
        "cache_scope": cache_scope_v,
        "status": _normalize_status(status),
        "title": title_v[:255],
        "summary": _clean_text(summary) or None,
        "content_md": content_v,
        "priority": max(1, min(int(priority or 50), 999)),
        "importance": max(1, min(int(importance or 3), 10)),
        "token_estimate": estimate_tokens(content_v),
        "entity_refs_json": _list_text(entity_refs),
        "theme_refs_json": _list_text(theme_refs),
        "metadata_json": _metadata(metadata),
    }
    row = (
        session.query(CloudAgentMemorySegment)
        .filter(CloudAgentMemorySegment.agent_key == agent_key_v, CloudAgentMemorySegment.segment_key == key_v)
        .first()
    )
    if row is None:
        row = CloudAgentMemorySegment(
            agent_key=agent_key_v,
            segment_key=key_v,
            created_at=now,
            updated_at=now,
            last_accessed_at=None,
            compressed_at=now if str(change_summary or "").lower().startswith("compress") else None,
            source_run_id=int(source_run_id) if source_run_id is not None else None,
            **values,
        )
        session.add(row)
        session.flush()
        _memory_event(
            session,
            agent_key=agent_key_v,
            segment_id=int(row.id),
            source_run_id=source_run_id,
            event_kind="segment_created",
            summary=change_summary or f"Created memory segment {key_v}.",
            payload={"segment_key": key_v, "segment_type": segment_type_v, "cache_scope": cache_scope_v,
                     "current": serialize_cloud_agent_memory_segment(row)},
        )
        return row, True

    changed = any(getattr(row, field) != value for field, value in values.items())
    if not changed:
        row.last_accessed_at = now
        return row, False
    previous = serialize_cloud_agent_memory_segment(row, include_content=False)
    previous_content = row.content_md
    for field, value in values.items():
        setattr(row, field, value)
    row.source_run_id = int(source_run_id) if source_run_id is not None else row.source_run_id
    row.updated_at = now
    row.last_accessed_at = now
    if "compress" in str(change_summary or "").lower():
        row.compressed_at = now
    session.flush()
    _memory_event(
        session,
        agent_key=agent_key_v,
        segment_id=int(row.id),
        source_run_id=source_run_id,
        event_kind="segment_updated",
        summary=change_summary or f"Updated memory segment {key_v}.",
        content_md=previous_content,
        payload={"segment_key": key_v, "previous": previous, "token_estimate": int(row.token_estimate or 0),
                 "current": serialize_cloud_agent_memory_segment(row)},
    )
    return row, True


def archive_cloud_agent_memory_segment(
    session: Session,
    *,
    agent_key: str | None,
    segment_key: str,
    source_run_id: int | None = None,
    reason: str | None = None,
) -> bool:
    agent_key_v = _normalize_agent_key(agent_key)
    key_v = str(segment_key or "").strip()
    if not key_v:
        return False
    row = (
        session.query(CloudAgentMemorySegment)
        .filter(CloudAgentMemorySegment.agent_key == agent_key_v, CloudAgentMemorySegment.segment_key == key_v)
        .first()
    )
    if row is None:
        return False
    if row.status == "archived":
        return False
    row.status = "archived"
    row.updated_at = utcnow()
    if source_run_id is not None:
        row.source_run_id = int(source_run_id)
    _memory_event(
        session,
        agent_key=agent_key_v,
        segment_id=int(row.id),
        source_run_id=source_run_id,
        event_kind="segment_archived",
        summary=reason or f"Archived memory segment {key_v}.",
        payload={"segment_key": key_v, "current": serialize_cloud_agent_memory_segment(row)},
    )
    return True


def list_cloud_agent_memory_segments(
    session: Session,
    *,
    agent_key: str | None = None,
    status: str = "active",
    query: str | None = None,
    segment_type: str | None = None,
    cache_scope: str | None = None,
    limit: int = 200,
) -> list[CloudAgentMemorySegment]:
    agent_key_v = _normalize_agent_key(agent_key)
    q = session.query(CloudAgentMemorySegment).filter(CloudAgentMemorySegment.agent_key == agent_key_v)
    if status and status not in {"all", "*"}:
        q = q.filter(CloudAgentMemorySegment.status == _normalize_status(status))
    if segment_type:
        q = q.filter(CloudAgentMemorySegment.segment_type == _normalize_segment_type(segment_type))
    if cache_scope:
        scope = _normalize_cache_scope(cache_scope, segment_type=_normalize_segment_type(segment_type or "working_model"))
        q = q.filter(CloudAgentMemorySegment.cache_scope == scope)
    text = _clean_text(query)
    if text:
        pattern = f"%{text.lower()}%"
        q = q.filter(
            or_(
                CloudAgentMemorySegment.segment_key.ilike(pattern),
                CloudAgentMemorySegment.title.ilike(pattern),
                CloudAgentMemorySegment.summary.ilike(pattern),
                CloudAgentMemorySegment.content_md.ilike(pattern),
            )
        )
    return q.order_by(
        case(_MEMORY_SCOPE_ORDER, value=CloudAgentMemorySegment.cache_scope, else_=9),
        CloudAgentMemorySegment.priority, CloudAgentMemorySegment.segment_key,
    ).limit(max(1, min(int(limit or 200), 1000))).all()


class MemorySearchHit(NamedTuple):
    row: CloudAgentMemorySegment
    matched_terms: list[str]
    match_fields: list[str]
    relevance_score: int


def memory_query_terms(value: object, *, limit: int = 12) -> list[str]:
    """Tokenize a research query; short agent-native terms such as AI and 2nm survive."""
    raw = str(value or "").strip().lower()
    tokens = re.findall(r"[a-z0-9][a-z0-9+#./-]*", raw)
    terms: list[str] = []
    for token in tokens:
        token = token.strip("-./+")
        if len(token) < 2 or token in _MEMORY_SEARCH_STOPWORDS:
            continue
        if token not in terms:
            terms.append(token)
        if len(terms) >= limit:
            break
    if not terms and raw:
        return [raw[:120]]
    return terms


def _memory_term_present(field: str, term: str) -> bool:
    if len(term) >= 3:
        return term in field
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", field) is not None


def _memory_like_pattern(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _memory_recency_score(updated_at: datetime | None, *, now: datetime) -> int:
    if updated_at is None:
        return 0
    age_days = (now - updated_at).total_seconds() / 86400.0
    if age_days <= 7:
        return 2
    if age_days <= 30:
        return 1
    return 0


def _memory_updated_ts(value: datetime | None) -> float:
    if value is None:
        return 0.0
    return value.replace(tzinfo=timezone.utc).timestamp()


def search_cloud_agent_memory_segments(
    session: Session,
    *,
    agent_key: str | None = None,
    query: str | None = None,
    segment_type: str | None = None,
    status: str = "active",
    limit: int = 20,
) -> tuple[list[MemorySearchHit], dict[str, Any]]:
    """Rank belief memory by query relevance, then recency, then scope.

    Field weights: segment_key 6, title 5, summary 3, content 1; a verbatim
    phrase match adds 4 (key/title/summary) or 2 (content); recently revised
    segments get a small freshness bonus. Cache scope is a tiebreaker only, so
    a directly relevant newer belief is not buried under broad stable memory.
    With no query the newest segments per scope are returned, core/stable first.
    """
    agent_key_v = _normalize_agent_key(agent_key)
    q = session.query(CloudAgentMemorySegment).filter(CloudAgentMemorySegment.agent_key == agent_key_v)
    if status and status not in {"all", "*"}:
        q = q.filter(CloudAgentMemorySegment.status == _normalize_status(status))
    if segment_type:
        q = q.filter(CloudAgentMemorySegment.segment_type == _normalize_segment_type(segment_type))
    text = _clean_text(query)
    terms = memory_query_terms(text) if text else []
    if terms:
        clauses = []
        for term in terms:
            pattern = _memory_like_pattern(term)
            clauses.extend(
                [
                    CloudAgentMemorySegment.segment_key.ilike(pattern, escape="\\"),
                    CloudAgentMemorySegment.title.ilike(pattern, escape="\\"),
                    CloudAgentMemorySegment.summary.ilike(pattern, escape="\\"),
                    CloudAgentMemorySegment.content_md.ilike(pattern, escape="\\"),
                ]
            )
        q = q.filter(or_(*clauses))
    now = utcnow()
    phrase = text.lower()
    bounded_limit = max(1, min(int(limit or 20), 30))
    hits: list[MemorySearchHit] = []
    candidates = q.order_by(CloudAgentMemorySegment.updated_at.desc(), CloudAgentMemorySegment.id.desc()).limit(1001).all()
    retrieval_truncated = len(candidates) > 1000
    for row in candidates[:1000]:
        key = str(row.segment_key or "").lower()
        title = str(row.title or "").lower()
        summary = str(row.summary or "").lower()
        content = str(row.content_md or "").lower()
        if terms:
            score = 0
            matched: list[str] = []
            fields: set[str] = set()
            for term in terms:
                term_score = 0
                if _memory_term_present(key, term):
                    term_score += 6
                    fields.add("segment_key")
                if _memory_term_present(title, term):
                    term_score += 5
                    fields.add("title")
                if _memory_term_present(summary, term):
                    term_score += 3
                    fields.add("summary")
                if _memory_term_present(content, term):
                    term_score += 1
                    fields.add("content")
                if term_score:
                    matched.append(term)
                    score += term_score
            if not matched:
                continue
            if len(terms) > 1 and phrase:
                if any(_memory_term_present(field, phrase) for field in (key, title, summary)):
                    score += 4
                elif _memory_term_present(content, phrase):
                    score += 2
            score += _memory_recency_score(row.updated_at, now=now)
            hits.append(MemorySearchHit(row=row, matched_terms=matched, match_fields=sorted(fields), relevance_score=score))
        else:
            hits.append(MemorySearchHit(row=row, matched_terms=[], match_fields=[], relevance_score=0))
    if terms:
        hits.sort(
            key=lambda hit: (
                -len(hit.matched_terms),
                -hit.relevance_score,
                -_memory_updated_ts(hit.row.updated_at),
                _MEMORY_SCOPE_ORDER.get(str(hit.row.cache_scope), 9),
                hit.row.segment_key,
            )
        )
    else:
        hits.sort(
            key=lambda hit: (
                _MEMORY_SCOPE_ORDER.get(str(hit.row.cache_scope), 9),
                -_memory_updated_ts(hit.row.updated_at),
                hit.row.segment_key,
            )
        )
    matched_total = len(hits)
    full_match_total = sum(1 for hit in hits if len(hit.matched_terms) == len(terms)) if terms else matched_total
    meta = {
        "query": text,
        "query_terms": terms,
        "matched_total": matched_total,
        "full_match_total": full_match_total,
        "returned": min(matched_total, bounded_limit),
        "selection": "relevance_recency" if terms else "recency_within_scope",
        "retrieval_limit": 1000,
        "retrieval_truncated": retrieval_truncated,
        "counts_scope": "retrieved_candidates",
        "all_stored_segments_scanned": False,
    }
    return hits[:bounded_limit], meta


def render_cloud_agent_memory_context(
    session: Session,
    *,
    agent_key: str | None = None,
    max_chars: int | None = 160_000,
    mark_accessed: bool = True,
    cache_scopes: Iterable[str] | None = None,
    heading: str = "Cloud Agent Heartbeat Memory",
) -> tuple[str, dict[str, Any]]:
    agent_key_v = _normalize_agent_key(agent_key)
    rows = list_cloud_agent_memory_segments(session, agent_key=agent_key_v, status="active", limit=1000)
    scope_filter = {str(scope).strip().lower() for scope in (cache_scopes or []) if str(scope).strip()}
    if scope_filter:
        rows = [row for row in rows if str(row.cache_scope or "").strip().lower() in scope_filter]
    now = utcnow()
    if mark_accessed and rows:
        for row in rows:
            row.last_accessed_at = now
    return _render_cloud_agent_memory_rows(
        session,
        rows,
        agent_key=agent_key_v,
        max_chars=max_chars,
        heading=heading,
    )


def _render_cloud_agent_memory_rows(
    session: Session,
    rows: list[CloudAgentMemorySegment],
    *,
    agent_key: str,
    max_chars: int | None,
    heading: str,
) -> tuple[str, dict[str, Any]]:
    lines = [
        f"# {heading}",
        "",
        "This is private agent memory. It is not a report, thesis, note, knowledge asset, source pack, or user preference record.",
        "Use it as continuing working memory for heartbeat research. Prefer the stable memory above volatile recent context.",
        "",
    ]
    if not rows:
        lines.extend(
            [
                "No memory segments matched this cache scope.",
            ]
        )
        empty = "\n".join(lines).strip() + "\n"
        return empty, {"agent_key": agent_key, "segments": 0, "included_segments": 0, "omitted_segments": 0,
                       "token_estimate": count_tokens(empty), "cache_scopes": [], "budget": budget_status("memory_catalog", empty)}

    current_scope = None
    included = 0
    skipped = 0
    token_estimate = 0
    over_budget = False
    ceiling = POLICIES["memory_catalog"].ceiling_tokens
    projections = render_memory_projections(session, rows)
    compacted = 0
    source_locators = []
    for row in rows:
        segment_lines: list[str] = []
        if row.cache_scope != current_scope:
            segment_lines.extend(["", f"## {str(row.cache_scope).title()} Memory"])
        segment_lines.extend(["", f"### {row.title}", f"- key: `{row.segment_key}`", f"- type: `{row.segment_type}`"])
        if row.summary:
            segment_lines.append(f"- summary: {row.summary}")
        if row.entity_refs_json:
            segment_lines.append("- entities: " + ", ".join(row.entity_refs_json[:16]))
        if row.theme_refs_json:
            segment_lines.append("- themes: " + ", ".join(row.theme_refs_json[:16]))
        projection = projections[int(row.id)]
        content = projection["content_md"]
        segment_lines.append(f"- original: get_state_compaction_source(kind=belief, object_id={row.id}, content_field=content_md)")
        if projection.get("truncated"):
            segment_lines.append(f"- source continuation token: {projection['source_page']['next_offset_tokens']}")
        if content:
            segment_lines.extend(["", content])
        candidate = "\n".join(lines + segment_lines).strip() + "\n"
        if count_tokens(candidate) > ceiling - 160 or (max_chars is not None and max_chars > 0 and len(candidate) > max_chars):
            skipped += 1
            over_budget = True
            continue
        current_scope = row.cache_scope
        lines.extend(segment_lines)
        source_locators.append(projection["source_locator"])
        included += 1
        compacted += int(projection.get("compacted", False))
        token_estimate += count_tokens(content)

    rendered = "\n".join(lines).strip() + "\n"
    if skipped:
        rendered = rendered.rstrip() + f"\n\n[{skipped} selected memory segment(s) omitted due to the active representation budget; retrieve exact beliefs with get_worldview_memory and page originals with get_state_compaction_source.]\n"
    return rendered, {
        "agent_key": agent_key,
        "segments": len(rows),
        "included_segments": included,
        "omitted_segments": skipped,
        "over_budget": over_budget,
        "requires_compression": over_budget,
        "token_estimate": estimate_tokens(rendered),
        "segment_tokens": token_estimate,
        "budget": budget_status("memory_catalog", rendered),
        "compacted_segments": compacted,
        "source_locators": source_locators,
        "retrieval_limit": 1000,
        "retrieval_may_be_truncated": len(rows) == 1000,
        "all_stored_segments_scanned": False,
        "cache_scopes": sorted({str(row.cache_scope) for row in rows}),
    }


def render_cloud_agent_memory_cache_sections(
    session: Session,
    *,
    agent_key: str | None = None,
    max_chars: int | None = 160_000,
    mark_accessed: bool = True,
) -> tuple[str, str, dict[str, Any]]:
    agent_key_v = _normalize_agent_key(agent_key)
    rows = list_cloud_agent_memory_segments(session, agent_key=agent_key_v, status="active", limit=1000)
    now = utcnow()
    if mark_accessed and rows:
        for row in rows:
            row.last_accessed_at = now
    stable_rows = [row for row in rows if str(row.cache_scope or "").strip().lower() in {"core", "stable"}]
    volatile_rows = [row for row in rows if str(row.cache_scope or "").strip().lower() not in {"core", "stable"}]
    stable_md, stable_meta = _render_cloud_agent_memory_rows(
        session,
        stable_rows,
        agent_key=agent_key_v,
        max_chars=max_chars,
        heading="Cloud Agent Stable Heartbeat Memory",
    )
    volatile_md, volatile_meta = _render_cloud_agent_memory_rows(
        session,
        volatile_rows,
        agent_key=agent_key_v,
        max_chars=max_chars,
        heading="Cloud Agent Volatile Heartbeat Memory",
    )
    meta = {
        "agent_key": agent_key_v,
        "segments": len(rows),
        "stable": stable_meta,
        "volatile": volatile_meta,
        "included_segments": int(stable_meta.get("included_segments") or 0) + int(volatile_meta.get("included_segments") or 0),
        "omitted_segments": int(stable_meta.get("omitted_segments") or 0) + int(volatile_meta.get("omitted_segments") or 0),
        "token_estimate": estimate_tokens(stable_md) + estimate_tokens(volatile_md),
        "cache_scopes": sorted({str(row.cache_scope) for row in rows}),
        "stable_cache_scopes": ["core", "stable"],
        "volatile_cache_scopes": sorted({str(row.cache_scope) for row in volatile_rows}),
        "retrieval_limit": 1000,
        "retrieval_may_be_truncated": len(rows) == 1000,
        "all_stored_segments_scanned": False,
    }
    return stable_md, volatile_md, meta
