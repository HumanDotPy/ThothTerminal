"""Run-scoped belief memory for a scheduled Harness research turn."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.cloud_agent_memory_service import (
    CACHE_SCOPES,
    CLOUD_RESEARCH_HEARTBEAT_AGENT_KEY,
    SEGMENT_TYPES,
    archive_cloud_agent_memory_segment,
    search_cloud_agent_memory_segments,
    render_memory_projections,
    serialize_cloud_agent_memory_event,
    serialize_cloud_agent_memory_segment,
    upsert_cloud_agent_memory_segment,
)
from thoth_terminal.cloud_goal_run_service import append_cloud_goal_event
from thoth_terminal.models import CloudAgentMemoryEvent, CloudAgentMemorySegment
from thoth_terminal.research_state_tokens import POLICIES, budget_status, token_page
from thoth_terminal.worldview_write_service import _authorized_run, _replayed_receipt


def get_worldview_memory(
    session: Session, *, segment_key: str = "", query: str = "",
    segment_type: str = "", include_history: bool = False, limit: int = 20,
) -> dict[str, Any]:
    """Return bounded private belief state; history requires an exact key.

    Topic reads are tokenized, relevance-and-recency ranked, and report how
    many segments matched so the caller can tell a selection cap from absence.
    """
    key = str(segment_key or "").strip()
    bounded_limit = max(1, min(int(limit or 20), 30))
    if include_history and not key:
        raise ValueError("include_history requires segment_key")
    search_meta: dict[str, Any] = {"matched_total": 0, "query_terms": [], "selection": "exact_key"}
    match_info: dict[int, dict[str, Any]] = {}
    if key:
        rows = session.query(CloudAgentMemorySegment).filter_by(
            agent_key=CLOUD_RESEARCH_HEARTBEAT_AGENT_KEY, segment_key=key,
        ).limit(1).all()
        search_meta["matched_total"] = len(rows)
    else:
        hits, search_meta = search_cloud_agent_memory_segments(
            session, agent_key=CLOUD_RESEARCH_HEARTBEAT_AGENT_KEY,
            query=str(query or ""), segment_type=str(segment_type or ""),
            limit=bounded_limit,
        )
        rows = [hit.row for hit in hits]
        match_info = {
            int(hit.row.id): {"matched_terms": hit.matched_terms, "match_fields": hit.match_fields,
                              "relevance_score": hit.relevance_score}
            for hit in hits
        }
    segments = []
    projections = render_memory_projections(session, rows[:bounded_limit])
    for row in rows[:bounded_limit]:
        payload = serialize_cloud_agent_memory_segment(row, match=match_info.get(int(row.id)))
        payload.update(projections[int(row.id)])
        segments.append(payload)
    history: list[dict[str, Any]] = []
    if include_history and rows:
        events = (
            session.query(CloudAgentMemoryEvent)
            .filter_by(agent_key=CLOUD_RESEARCH_HEARTBEAT_AGENT_KEY, segment_id=rows[0].id)
            .order_by(CloudAgentMemoryEvent.id.desc()).limit(min(bounded_limit, 20)).all()
        )
        for event in events:
            payload = serialize_cloud_agent_memory_event(event)
            content = str(payload.get("content_md") or "")
            page = token_page(content, limit_tokens=POLICIES["belief"].ceiling_tokens)
            payload.update(content_md=page["text"], content_truncated=page["truncated"],
                           source_page={name: value for name, value in page.items() if name != "text"},
                           budget=budget_status("belief", content),
                           source_locator={"kind": "belief_history", "object_id": int(event.id),
                                           "content_field": "content_md"})
            current = payload["payload"].get("current")
            if isinstance(current, dict):
                current_content = str(current.get("content_md") or "")
                current_page = token_page(current_content, limit_tokens=POLICIES["belief"].ceiling_tokens)
                current.update(content_md=current_page["text"], content_truncated=current_page["truncated"],
                               source_page={name: value for name, value in current_page.items() if name != "text"},
                               budget=budget_status("belief", current_content),
                               source_locator={"kind": "belief_history", "object_id": int(event.id),
                                               "content_field": "current.content_md"})
            history.append(payload)
    matched_total = int(search_meta.get("matched_total") or 0)
    return {"agent_key": CLOUD_RESEARCH_HEARTBEAT_AGENT_KEY, "segments": segments,
            "history": history, "count": len(segments), "limit": bounded_limit,
            "matched_total": matched_total,
            "full_match_total": int(search_meta.get("full_match_total") or matched_total),
            "query_terms": list(search_meta.get("query_terms") or []),
            "selection": str(search_meta.get("selection") or ""),
            "retrieval_truncated": bool(search_meta.get("retrieval_truncated")),
            "counts_scope": search_meta.get("counts_scope", "exact_key"),
            "truncated": matched_total > len(segments) or bool(search_meta.get("retrieval_truncated"))}


def apply_worldview_memory(
    session: Session, *, cloud_run_id: int, capability: str,
    operation_key: str, payload: dict[str, Any],
) -> dict[str, Any]:
    """Revise one belief, preserving its previous version and a replayable receipt."""
    if not 1 <= len(str(operation_key or "").strip()) <= 120:
        raise ValueError("operation_key must be 1-120 characters")
    if not isinstance(payload, dict) or len(json.dumps(payload, ensure_ascii=False, default=str)) > 20_000:
        raise ValueError("Memory payload must be an object under 20,000 characters")
    run = _authorized_run(session, cloud_run_id=cloud_run_id, capability=capability)
    replay = _replayed_receipt(
        session, cloud_run_id=cloud_run_id,
        event_kind="worldview_memory_write", operation_key=operation_key,
    )
    if replay is not None:
        return replay
    action = str(payload.get("action") or "").strip()
    key = str(payload.get("segment_key") or "").strip()
    if not 1 <= len(key) <= 255:
        raise ValueError("segment_key must be 1-255 characters")
    if action == "upsert":
        title = str(payload.get("title") or "").strip()
        content = str(payload.get("content_md") or "").strip()
        segment_type = str(payload.get("segment_type") or "working_model").strip()
        cache_scope = str(payload.get("cache_scope") or "").strip()
        if segment_type not in SEGMENT_TYPES or (cache_scope and cache_scope not in CACHE_SCOPES):
            raise ValueError("segment_type or cache_scope is unsupported")
        if not title or len(title) > 255 or not content or len(content) > 12_000:
            raise ValueError("title and content_md are required; title is capped at 255 and content_md at 12,000 characters")
        if payload.get("metadata") is not None and not isinstance(payload["metadata"], dict):
            raise ValueError("metadata must be an object")
        if not str(payload.get("change_summary") or "").strip():
            raise ValueError("change_summary is required")
        row, changed = upsert_cloud_agent_memory_segment(
            session, agent_key=CLOUD_RESEARCH_HEARTBEAT_AGENT_KEY,
            segment_key=key, segment_type=segment_type,
            title=title, content_md=content,
            summary=str(payload.get("summary") or "")[:1000],
            cache_scope=cache_scope,
            priority=int(payload.get("priority") or 50),
            importance=int(payload.get("importance") or 3),
            entity_refs=payload.get("entity_refs"), theme_refs=payload.get("theme_refs"),
            metadata=payload.get("metadata"), source_run_id=int(run.id),
            change_summary=str(payload["change_summary"])[:1000],
        )
        result = {"action": action, "segment_key": key, "segment_id": int(row.id),
                  "changed": changed, "status": row.status, "updated_at": row.updated_at.isoformat(),
                  **{name: value for name, value in render_memory_projections(session, [row])[int(row.id)].items()
                     if name in {"budget", "source_locator", "source_hash", "compacted", "mode"}}}
    elif action == "archive":
        reason = str(payload.get("change_summary") or "").strip()
        if not reason:
            raise ValueError("change_summary is required")
        changed = archive_cloud_agent_memory_segment(
            session, agent_key=CLOUD_RESEARCH_HEARTBEAT_AGENT_KEY,
            segment_key=key, source_run_id=int(run.id), reason=reason[:1000],
        )
        result = {"action": action, "segment_key": key, "changed": changed}
        row = session.query(CloudAgentMemorySegment).filter_by(
            agent_key=CLOUD_RESEARCH_HEARTBEAT_AGENT_KEY, segment_key=key).first()
        if row is not None:
            result.update(segment_id=int(row.id), budget=budget_status("belief", row.content_md or ""),
                          source_locator={"kind": "belief", "object_id": int(row.id), "content_field": "content_md"})
    else:
        raise ValueError("action must be upsert or archive")
    if changed:
        # Production disables autoflush. Persist the pending history event in
        # this transaction before querying for the immutable revision receipt.
        session.flush()
        # Read immediately after the mutation: another write to this key in the
        # same run must receive its own event rather than a mutable segment ID.
        memory_event = session.query(CloudAgentMemoryEvent).filter_by(
            agent_key=CLOUD_RESEARCH_HEARTBEAT_AGENT_KEY, segment_id=int(row.id),
            source_run_id=int(run.id),
        ).order_by(CloudAgentMemoryEvent.id.desc()).first()
        if memory_event is None:
            raise ValueError("Changed memory write has no version event")
        frozen_content = str(memory_event.payload_json["current"]["content_md"])
        result.update(memory_event_id=int(memory_event.id),
                      after_content_sha256=hashlib.sha256(frozen_content.encode("utf-8")).hexdigest())
    receipt = {"cloud_run_id": int(run.id), "operation_key": operation_key,
               **result, "replayed": False}
    append_cloud_goal_event(
        session, run, event_kind="worldview_memory_write",
        summary=str(payload.get("change_summary") or "Belief memory updated")[:500],
        payload={"operation_key": operation_key, "receipt": receipt},
    )
    return receipt
