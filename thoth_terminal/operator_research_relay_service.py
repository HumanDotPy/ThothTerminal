"""Attributed operator conversations on the existing evidence intake rails.

Messages are immutable documents and requests. Request status is a mutable
work queue projection; replies always receive a new ID for cursor readers.
Authentication of an owner connection or scheduled run belongs to its caller.
"""

from __future__ import annotations

import hashlib
import json
import math

from sqlalchemy import and_, func, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.models import (
    AnalystEvidence, CloudAgentMemoryEvent, CloudAgentMemorySegment, CloudGoalEvent, CloudGoalRun, CloudPromptStateSnapshot,
    DatasetVersion, Document, ExternalAgentEvidence,
    ExternalAgentPrincipal, ExternalAgentResearchRequest, InformalResearchDocumentEvent,
    LocalAgentWriteDiff, ProductConfigurationRevision, ProductModelRevision,
    ResearchExtension, ResearchExtensionEvent, ResearchExtensionVersion, ResearchSubjectScenario, RoadmapMention, Source,
)

_META = "operator_research_relay"
_SOURCE = "Operator research conversation"
_INCOMING_KINDS = {"observation", "interpretation", "feedback", "question", "answer"}
_OUTGOING_KINDS = {"question", "response", "change_note", "capability_request", "followup"}
_WAITING_KINDS = {"question", "capability_request"}
_ASSESSMENTS = {"helpful", "harmful", "mixed", "inconclusive"}
_CANONICAL_TARGETS = {
    "worldview_write": ("write_diff_id", LocalAgentWriteDiff),
    "worldview_scenario_write": ("scenario_id", ResearchSubjectScenario),
    "worldview_consensus_evidence": ("analyst_evidence_id", AnalystEvidence),
    "worldview_roadmap_update": ("mention_id", RoadmapMention),
    "worldview_roadmap_review": ("mention_id", RoadmapMention),
    "worldview_journal_write": ("event_id", InformalResearchDocumentEvent),
    "worldview_product_model_write": ("model_revision_id", ProductModelRevision),
    "worldview_product_configuration_write": ("configuration_revision_id", ProductConfigurationRevision),
    "worldview_memory_write": ("segment_id", CloudAgentMemorySegment),
    "worldview_agentx_configuration_write": ("version_id", DatasetVersion),
    "research_state_compaction": ("snapshot_id", CloudPromptStateSnapshot),
}


def _assessment(value: str | None) -> str | None:
    if value is not None and (not isinstance(value, str) or value not in _ASSESSMENTS):
        raise ValueError("assessment must be helpful, harmful, mixed, or inconclusive")
    return value


def _related_writes(session: Session, reply: ExternalAgentResearchRequest | None) -> tuple[list[dict], bool]:
    """Carry exact prior write identities into feedback without claiming new writes."""
    metadata = _metadata(session, reply) if reply is not None else {}
    references = metadata.get("write_references", []) + metadata.get("related_write_references", [])
    unique = {}
    for reference in references:
        unique[json.dumps(reference, sort_keys=True, separators=(",", ":"))] = reference
    # Bound chains, retaining every link through its immutable parent message.
    return list(unique.values())[:40], len(unique) > 40 or bool(metadata.get("related_references_truncated"))


def _text(value: object, name: str, maximum: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    result = value.strip()
    if (required and not result) or len(result) > maximum:
        raise ValueError(f"{name} must be {'1' if required else '0'}-{maximum} characters")
    return result


def _id(value: object, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _document_ids(session: Session, values: list[int] | None) -> list[int]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > 20:
        raise ValueError("source_document_ids must be a list of at most 20 IDs")
    ids = sorted({_id(value, "source_document_id") for value in values})
    found = {row[0] for row in session.query(Document.id).filter(Document.id.in_(ids)).all()}
    if found != set(ids):
        raise ValueError("source_document_ids must identify stored documents")
    return ids


def _owner(session: Session, *, create: bool = False) -> ExternalAgentPrincipal | None:
    if create:
        dialect = session.get_bind().dialect.name
        insert = pg_insert if dialect == "postgresql" else sqlite_insert if dialect == "sqlite" else None
        if insert is None:
            raise ValueError("Operator research relay requires PostgreSQL or SQLite")
        now = utcnow()
        session.execute(insert(ExternalAgentPrincipal).values(
            slug="owner-agent", display_name="Owner agent", status="active",
            created_at=now, updated_at=now,
        ).on_conflict_do_nothing(index_elements=["slug"]))
    query = session.query(ExternalAgentPrincipal).filter_by(slug="owner-agent")
    row = (query.with_for_update() if create else query).first()
    if row is not None and row.status != "active":
        raise ValueError("Owner agent principal is disabled")
    return row


def _request(session: Session, owner: ExternalAgentPrincipal | None, request_id: int) -> ExternalAgentResearchRequest:
    _id(request_id, "request_id")
    row = session.query(ExternalAgentResearchRequest).filter_by(
        id=request_id, principal_id=owner.id if owner else -1,
    ).first()
    if row is None:
        raise ValueError("Operator research request not found")
    return row


def _metadata(session: Session, row: ExternalAgentResearchRequest) -> dict:
    document = session.get(Document, row.document_id)
    return dict((document.extra or {}).get(_META) or {})


def _serialize(session: Session, row: ExternalAgentResearchRequest, *, compact: bool = False) -> dict:
    document = session.get(Document, row.document_id)
    metadata = dict((document.extra or {}).get(_META) or {})
    authored = bool(metadata)
    text = document.raw_text if authored else row.request_text
    rationale = str(metadata.get("rationale") or "")
    result = {
        "request_id": int(row.id), "document_id": int(document.id),
        "request_key": row.request_key, "document_title": (document.title or "")[:512],
        "document_kind": document.kind, "kind": metadata.get("kind", "source_request"),
        "direction": metadata.get("direction", "incoming"),
        "speaker": metadata.get("speaker", "operator"), "status": row.status,
        "text": text[:1200 if compact else 16000],
        "rationale": rationale[:600 if compact else 4000],
        "text_truncated": len(text) > (1200 if compact else 16000),
        "rationale_truncated": len(rationale) > (600 if compact else 4000),
        "document_excerpt": (document.normalized_text or "")[:1200],
        "created_at": row.created_at.isoformat(),
        "source_document_ids": metadata.get("source_document_ids", [int(document.id)]),
        "confidence": metadata.get("confidence"),
        "reply_to_request_id": metadata.get("reply_to_request_id"),
        "supersedes_request_id": metadata.get("supersedes_request_id"),
        "cloud_run_id": metadata.get("cloud_run_id"),
        "disposition": metadata.get("disposition"),
        "write_references": metadata.get("write_references", []),
        "related_write_references": metadata.get("related_write_references", []),
        "related_references_truncated": metadata.get("related_references_truncated", False),
        "title": metadata.get("title") or document.title,
        "assessment": metadata.get("assessment"), "assessment_is_verified": False,
        "rights_class": (document.extra or {}).get("rights_class"),
        "claim_basis": metadata.get("claim_basis", "source_document"),
        "attribution_is_claimed": True,
    }
    return result


def _persist(session: Session, *, owner: ExternalAgentPrincipal, key: str, payload: dict,
             status: str) -> tuple[ExternalAgentResearchRequest, bool]:
    authored_payload = {key: value for key, value in payload.items() if key != "write_references"}
    digest = hashlib.sha256(json.dumps(authored_payload, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    previous = session.query(ExternalAgentResearchRequest).filter_by(
        principal_id=owner.id, request_key=key,
    ).first()
    if previous is not None:
        if _metadata(session, previous).get("payload_sha256") != digest:
            raise ValueError("request_key or operation_key was already used for a different message")
        return previous, True
    now = utcnow()
    source = session.query(Source).filter_by(name=_SOURCE).first()
    if source is None:
        source = Source(name=_SOURCE, category="operator_context", priority=1, created_at=now,
                        config={"connector": "operator_research_relay", "enabled": False,
                                "source_quality": {"rights_class": "private"}})
        session.add(source)
        session.flush()
    metadata = {**payload, "payload_sha256": digest}
    document = Document(
        kind="operator_feedback" if payload["direction"] == "incoming" else f"operator_{payload['kind']}",
        source_id=source.id, external_id=f"operator-relay:{owner.id}:{digest}",
        title=payload.get("title") or f"{payload['speaker']}: {payload['kind']}", author=payload["speaker"],
        ingested_at=now, raw_text=payload["text"], normalized_text=payload["text"],
        content_hash=hashlib.sha256(payload["text"].encode()).hexdigest(),
        triage_state="IGNORED", triage_updated_at=now, ignored_at=now,
        extra={"rights_class": "private", "access_scope": "operator",
               "owner_principal_id": int(owner.id),
               "origin": "operator" if payload["direction"] == "incoming" else "research_agent",
               "acquisition_mode": "operator_conversation", _META: metadata},
    )
    session.add(document)
    session.flush()
    session.add(ExternalAgentEvidence(principal_id=owner.id, document_id=document.id, created_at=now))
    row = ExternalAgentResearchRequest(principal_id=owner.id, document_id=document.id,
                                       request_key=key, request_text=payload["text"],
                                       status=status, created_at=now)
    session.add(row)
    session.flush()
    return row, False


def submit_operator_feedback(
    session: Session, *, request_key: str, kind: str, text: str, speaker: str = "operator",
    rationale: str = "", confidence: float | None = None,
    source_document_ids: list[int] | None = None, reply_to_request_id: int | None = None,
    supersedes_request_id: int | None = None,
    assessment: str | None = None,
) -> dict:
    """Preserve an attributed observation, opinion, question, or answer."""
    key = _text(request_key, "request_key", 120)
    if kind not in _INCOMING_KINDS:
        raise ValueError("Invalid operator feedback kind")
    if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                                   or not math.isfinite(confidence) or not 0 <= confidence <= 1):
        raise ValueError("confidence must be a finite number between 0 and 1")
    payload = {"direction": "incoming", "kind": kind, "text": _text(text, "text", 16000),
               "speaker": _text(speaker, "speaker", 120),
               "rationale": _text(rationale, "rationale", 4000, required=False),
               "confidence": float(confidence) if confidence is not None else None,
               "claim_basis": "operator_authored_context",
               "source_document_ids": _document_ids(session, source_document_ids),
               "reply_to_request_id": reply_to_request_id, "supersedes_request_id": supersedes_request_id,
               "assessment": _assessment(assessment)}
    owner = _owner(session, create=True)
    reply = _request(session, owner, reply_to_request_id) if reply_to_request_id is not None else None
    if kind == "answer" and (reply is None or _metadata(session, reply).get("direction") != "outgoing"
                             or _metadata(session, reply).get("kind") not in _WAITING_KINDS):
        raise ValueError("An answer must reply to an outgoing operator question or capability request")
    if assessment is not None and (kind not in {"feedback", "answer"} or reply is None):
        raise ValueError("An assessment requires feedback or an answer linked to a prior message")
    payload["related_write_references"], payload["related_references_truncated"] = _related_writes(session, reply)
    prior = None
    if supersedes_request_id is not None:
        prior = _request(session, owner, supersedes_request_id)
        if _metadata(session, prior).get("direction") != "incoming":
            raise ValueError("A revision must supersede an incoming operator message")
    row, replayed = _persist(session, owner=owner, key=key, payload=payload, status="submitted")
    if not replayed and prior is not None and prior.status in {"submitted", "waiting"}:
        prior.status = "superseded"
        prior.resolved_at = utcnow()
        session.flush()
    if not replayed and reply is not None and _metadata(session, reply).get("direction") == "outgoing" and _metadata(session, reply).get("kind") in _WAITING_KINDS and kind == "answer":
        reply.status = "researched"
        reply.resolved_at = utcnow()
        session.flush()
    return {**_serialize(session, row), "replayed": replayed}


def _verified_writes(session: Session, run_id: int, keys: list[str]) -> list[dict]:
    if not keys:
        raise ValueError("A change note or updated response requires verified write_operation_keys")
    events = session.query(CloudGoalEvent).filter(
        CloudGoalEvent.run_id == run_id, CloudGoalEvent.event_kind.in_(_CANONICAL_TARGETS),
        CloudGoalEvent.payload["operation_key"].as_string().in_(keys),
    ).order_by(CloudGoalEvent.id.asc()).all()
    found = {}
    for event in events:
        payload = dict(event.payload or {})
        key = payload.get("operation_key")
        receipt = dict(payload.get("receipt") or {})
        field, model = _CANONICAL_TARGETS[event.event_kind]
        if receipt.get("changed") is False:
            continue
        if event.event_kind in {"worldview_consensus_evidence", "worldview_roadmap_review"} and receipt.get("created") is False:
            continue
        if event.event_kind == "worldview_roadmap_update" and not (receipt.get("mention_created") or receipt.get("material_changed")):
            continue
        if receipt.get("cloud_run_id", run_id) != run_id or receipt.get("operation_key", key) != key:
            continue
        if event.event_kind == "worldview_memory_write":
            target = session.get(CloudAgentMemorySegment, receipt.get("segment_id"))
            mutation_id = receipt.get("memory_event_id")
            mutation = session.get(CloudAgentMemoryEvent, mutation_id) if type(mutation_id) is int else None
            current = (mutation.payload_json or {}).get("current") if mutation is not None else None
            if (not target or target.agent_key != "cloud_research_heartbeat" or not mutation
                    or mutation.source_run_id != run_id or mutation.segment_id != target.id
                    or mutation.event_kind not in {"segment_created", "segment_updated", "segment_archived"}
                    or not isinstance(current, dict)
                    or hashlib.sha256(str(current.get("content_md") or "").encode()).hexdigest() != receipt.get("after_content_sha256")):
                continue
            target_id = int(target.id)
        else:
            target_id = receipt.get(field)
            target = session.get(model, target_id) if type(target_id) is int else None
            if target is None:
                continue
            if isinstance(target, (ProductModelRevision, ProductConfigurationRevision)) and target.created_by_run_id != run_id:
                continue
            if isinstance(target, InformalResearchDocumentEvent) and target.source_run_id != run_id:
                continue
            if isinstance(target, RoadmapMention) and target.cloud_goal_run_id != run_id:
                continue
            if isinstance(target, ResearchSubjectScenario) and (target.audit_json or {}).get("cloud_goal_run_id") != run_id:
                continue
            if isinstance(target, CloudPromptStateSnapshot) and (target.manifest_json or {}).get("cloud_run_id") != run_id:
                continue
        if key in found:
            raise ValueError("write_operation_key is ambiguous across canonical write events")
        found[key] = {"cloud_event_id": int(event.id), "event_kind": event.event_kind,
                      "cloud_run_id": run_id, "operation_key": key, field: target_id}
        if event.event_kind == "worldview_memory_write":
            found[key].update(memory_event_id=int(mutation.id), after_content_sha256=receipt["after_content_sha256"],
                              source_locator={"kind": "belief_history", "object_id": int(mutation.id),
                                              "content_field": "current.content_md"})
        elif event.event_kind == "research_state_compaction":
            found[key].update(source_hash=receipt.get("source_hash"), summary_tokens=receipt.get("summary_tokens"))
    # Extensions have their own immutable lifecycle ledger, not CloudGoalEvents.
    extensions = session.query(ResearchExtensionEvent).filter(
        ResearchExtensionEvent.cloud_run_id == run_id, ResearchExtensionEvent.operation_key.in_(keys),
        ResearchExtensionEvent.action.in_({"create", "activate", "rollback"}),
    ).all()
    for event in extensions:
        extension = session.get(ResearchExtension, event.extension_id)
        version_id = event.active_version_id if event.action in {"activate", "rollback"} else event.candidate_id
        version = session.get(ResearchExtensionVersion, version_id) if version_id else None
        if extension is None or (version_id and (version is None or version.extension_id != extension.id)):
            continue
        if event.operation_key in found:
            raise ValueError("write_operation_key is ambiguous across canonical and extension ledgers")
        reference = {"extension_event_id": int(event.id), "event_kind": "research_extension_" + event.action,
                     "operation_key": event.operation_key, "cloud_run_id": run_id,
                     "extension_key": extension.capability, "action": event.action, "version_id": version_id}
        if version is not None:
            from thoth_terminal.research_extension_service import _assert_version_integrity
            _assert_version_integrity(session, version)
            reference.update(content_sha256=version.content_sha256, version=version.version)
        if event.action == "activate":
            reference.update(comparison_provenance=(event.receipt_json or {}).get("comparison_provenance"),
                             comparison_case_count=(event.receipt_json or {}).get("comparison_case_count"))
        found[event.operation_key] = reference
    if set(found) != set(keys):
        raise ValueError("write_operation_keys must reference actual canonical writes in the same run")
    return [found[key] for key in keys]


def record_worldview_operator_message(
    session: Session, *, cloud_run_id: int, operation_key: str, kind: str, text: str,
    rationale: str, source_document_ids: list[int] | None = None,
    reply_to_request_id: int | None = None, disposition: str | None = None,
    write_operation_keys: list[str] | None = None,
    title: str | None = None, assessment: str | None = None,
) -> dict:
    """Publish attributed posts, follow-ups, capability requests and replies."""
    run_id = _id(cloud_run_id, "cloud_run_id")
    if session.get(CloudGoalRun, run_id) is None:
        raise ValueError("Worldview run not found")
    operation = _text(operation_key, "operation_key", 120)
    if kind not in _OUTGOING_KINDS:
        raise ValueError("Invalid outgoing research message kind")
    if write_operation_keys is not None and (not isinstance(write_operation_keys, list) or len(write_operation_keys) > 20):
        raise ValueError("write_operation_keys must be a list of at most 20 keys")
    keys = sorted({_text(key, "write_operation_key", 120) for key in (write_operation_keys or [])})
    if kind == "response" and (reply_to_request_id is None or disposition not in {"updated", "considered"}):
        raise ValueError("A response requires an intake request and updated/considered disposition")
    if kind == "question" and (disposition is not None or keys):
        raise ValueError("A question cannot claim a disposition or canonical writes")
    if kind != "response" and disposition is not None:
        raise ValueError("Only a response can claim a disposition")
    if disposition == "considered" and keys:
        raise ValueError("A considered response cannot claim canonical writes")
    if kind == "followup" and reply_to_request_id is None:
        raise ValueError("A followup must link a prior post, request, or feedback message")
    if assessment is not None and kind != "followup":
        raise ValueError("An outgoing assessment belongs to a followup")
    owner = _owner(session, create=True)
    reply = _request(session, owner, reply_to_request_id) if reply_to_request_id is not None else None
    if kind in {"question", "response"} and reply is not None and _metadata(session, reply).get("direction", "incoming") != "incoming":
        raise ValueError("Research must reply to an incoming owner intake")
    refs = _verified_writes(session, run_id, keys) if kind == "change_note" or keys or disposition == "updated" else []
    related, related_truncated = _related_writes(session, reply)
    payload = {"direction": "outgoing", "kind": kind, "speaker": "worldview_agent",
               "text": _text(text, "text", 16000), "rationale": _text(rationale, "rationale", 4000),
               "title": _text(title, "title", 255) if title is not None else None,
               "assessment": _assessment(assessment),
               "source_document_ids": _document_ids(session, source_document_ids),
               "reply_to_request_id": reply_to_request_id, "cloud_run_id": run_id,
               "operation_key": operation, "disposition": disposition, "write_references": refs,
               "write_operation_keys": keys, "claim_basis": "research_agent_message",
               "related_write_references": related, "related_references_truncated": related_truncated}
    # Bind outgoing idempotency to the scheduled run and operation.
    key = "worldview-relay:" + hashlib.sha256(f"{run_id}:{operation}".encode()).hexdigest()
    row, replayed = _persist(session, owner=owner, key=key, payload=payload,
                             status="waiting" if kind in _WAITING_KINDS else "published")
    if not replayed and reply is not None and _metadata(session, reply).get("direction", "incoming") == "incoming":
        reply.status = "waiting" if kind in _WAITING_KINDS else "reviewed" if disposition == "updated" or kind in {"change_note", "followup"} else "considered"
        reply.operator_note = payload["rationale"]
        reply.resolved_at = None if kind in _WAITING_KINDS else utcnow()
        reply.resolution_operation_key = f"operator-response:{row.id}" if kind == "response" else None
        if kind == "response":
            # A final answer also closes the waiting intake that prompted an
            # intervening research question. Immutable reply IDs carry lineage.
            ancestor = reply
            visited = {int(reply.id)}
            for _ in range(100):
                parent_id = _metadata(session, ancestor).get("reply_to_request_id")
                if type(parent_id) is not int or parent_id in visited:
                    break
                ancestor = _request(session, owner, parent_id)
                visited.add(parent_id)
                if ancestor.status == "waiting" and _metadata(session, ancestor).get("direction", "incoming") == "incoming":
                    ancestor.status = reply.status
                    ancestor.operator_note = payload["rationale"]
                    ancestor.resolved_at = reply.resolved_at
                    ancestor.resolution_operation_key = reply.resolution_operation_key
        session.flush()
    return {**_serialize(session, row), "replayed": replayed}


def get_operator_research_message(session: Session, request_id: int) -> dict:
    return _serialize(session, _request(session, _owner(session), request_id))


def get_operator_research_inbox(
    session: Session, *, after_request_id: int = 0, limit: int = 20, pending_only: bool = False,
) -> dict:
    if type(after_request_id) is not int or after_request_id < 0:
        raise ValueError("after_request_id must be a nonnegative integer")
    if type(limit) is not int or limit < 1:
        raise ValueError("limit must be a positive integer")
    bounded_limit = min(limit, 50)
    owner = _owner(session)
    query = session.query(ExternalAgentResearchRequest).filter(
        ExternalAgentResearchRequest.principal_id == (owner.id if owner else -1),
        ExternalAgentResearchRequest.id > after_request_id,
    )
    if pending_only:
        # Incoming submissions and outgoing waiting questions/capability requests are pending.
        # Every outgoing message has a Document metadata direction.
        query = query.join(Document, Document.id == ExternalAgentResearchRequest.document_id).filter(or_(
            and_(ExternalAgentResearchRequest.status == "submitted",
                 or_(Document.extra[_META]["direction"].as_string() == "incoming",
                     Document.extra[_META]["direction"].as_string().is_(None))),
            and_(ExternalAgentResearchRequest.status == "waiting",
                 Document.extra[_META]["direction"].as_string() == "outgoing"),
        ))
    total, latest_id = query.with_entities(
        func.count(ExternalAgentResearchRequest.id), func.max(ExternalAgentResearchRequest.id),
    ).one()
    rows = query.order_by(ExternalAgentResearchRequest.id.asc()).limit(bounded_limit + 1).all()
    has_more = len(rows) > bounded_limit
    rows = rows[:bounded_limit]
    return {"messages": [_serialize(session, row, compact=True) for row in rows],
            "count": len(rows), "total": int(total), "latest_request_id": int(latest_id or 0),
            "limit": bounded_limit, "after_request_id": after_request_id,
            "next_cursor": int(rows[-1].id) if rows else after_request_id,
            "has_more": has_more, "truncated": has_more or limit > bounded_limit}
