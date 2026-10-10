"""Read-only research desk over existing dispatches and durable change records."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime
from urllib.parse import quote

from sqlalchemy import and_, func, literal, or_, select, union_all
from sqlalchemy.orm import Session, defer

from thoth_terminal.models import (
    AnalystEvidence, CloudAgentMemorySegment, CloudGoalEvent, CloudGoalRun,
    Document, ExternalAgentPrincipal, ExternalAgentResearchRequest,
    InformalResearchDocument, LocalAgentWriteDiff, Note, AgentChangeEvent,
    ProductConfigurationRevision, ProductModelRevision, ResearchExtension,
    ResearchExtensionEvent, ResearchExtensionVersion, ResearchReport, ResearchSubject,
    ResearchSubjectScenario, RoadmapEntry, RoadmapMention,
)


# Ordinary tool calls, prompts, failures and private conversation events are not changes.
_CLOUD_CHANGES = (
    "worldview_scenario_write",
    "worldview_journal_write", "worldview_memory_write",
)
_EXTENSION_TRANSITIONS = ("create", "validate", "activate", "rollback")
_IMPROVEMENT_KINDS = ("change_note", "capability_request", "followup")
_SOURCES = {
    "write": LocalAgentWriteDiff, "consensus": AnalystEvidence,
    "roadmap": RoadmapMention, "model": ProductModelRevision,
    "configuration": ProductConfigurationRevision, "extension": ResearchExtensionEvent,
    "world": CloudGoalEvent,
    "note": Note, "change": AgentChangeEvent, "improvement": ExternalAgentResearchRequest,
}
_TARGET_PATHS = {
    "note": "/notes/{id}", "research_doc": "/reports/{id}",
    "informal_research_doc": "/informal-research/{id}",
    "informal_research_document": "/informal-research/{id}",
    "dataset": "/datasets/{id}", "thesis": "/theses/{id}",
}


def _text(value, limit=1200):
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _iso(value):
    return value.isoformat() + "Z" if value else None


def _cursor(mode, row):
    data = [mode, row.created_at.isoformat(), row.kind, row.id]
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def _decode_cursor(value, mode):
    if not value:
        return None
    try:
        if not isinstance(value, str) or len(value) > 400:
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        cursor_mode, stamp, kind, number = data
        if cursor_mode != mode or type(number) is not int or number < 1:
            raise ValueError
        if kind not in (_SOURCES if mode == "operator" else {"dispatch"}):
            raise ValueError
        date = datetime.fromisoformat(stamp)
        if date.tzinfo is not None:
            raise ValueError
        return date, kind, number
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError("Invalid research desk cursor") from exc


def _operator_messages(statement, *, kinds=None, outgoing=True):
    """Scope authored private messages before pagination or detail hydration."""
    metadata = Document.extra["operator_research_relay"]
    statement = statement.join(Document, Document.id == ExternalAgentResearchRequest.document_id).join(
        ExternalAgentPrincipal, ExternalAgentPrincipal.id == ExternalAgentResearchRequest.principal_id,
    ).where(
        ExternalAgentPrincipal.slug == "owner-agent", ExternalAgentPrincipal.status == "active",
        Document.extra["rights_class"].as_string() == "private",
        Document.extra["access_scope"].as_string() == "operator",
        Document.extra["acquisition_mode"].as_string() == "operator_conversation",
        Document.extra["owner_principal_id"].as_integer() == ExternalAgentPrincipal.id,
        metadata["direction"].as_string().in_(("outgoing",) if outgoing else ("incoming", "outgoing")),
    )
    if kinds is not None:
        statement = statement.where(metadata["kind"].as_string().in_(kinds))
    return statement


def _page(session, mode, limit, before_id):
    if mode == "minimal":
        query = select(literal("dispatch").label("kind"), CloudGoalRun.id.label("id"),
                       func.coalesce(CloudGoalRun.completed_at, CloudGoalRun.created_at).label("created_at")).where(
            CloudGoalRun.task_kind == "cloud_research_heartbeat",
            CloudGoalRun.status == "complete",
        )
    else:
        parts = []
        for kind, model in _SOURCES.items():
            part = select(literal(kind).label("kind"), model.id.label("id"),
                          model.created_at.label("created_at"))
            if kind == "world":
                part = part.where(model.event_kind.in_(_CLOUD_CHANGES))
            if kind == "extension":
                part = part.where(model.action.in_(_EXTENSION_TRANSITIONS))
            if kind == "improvement":
                part = _operator_messages(part, kinds=_IMPROVEMENT_KINDS)
            if kind == "write":
                # These destinations have their own canonical immutable revision/mention rail.
                part = part.where(~model.destination_type.in_(
                    ("analyst_evidence", "roadmap", "roadmap_entry", "daily_summary")))
            parts.append(part)
        query = union_all(*parts)
    feed = query.subquery()
    statement = select(feed)
    cursor = _decode_cursor(before_id, mode)
    if cursor:
        stamp, kind, number = cursor
        statement = statement.where(or_(
            feed.c.created_at < stamp,
            and_(feed.c.created_at == stamp, feed.c.kind < kind),
            and_(feed.c.created_at == stamp, feed.c.kind == kind, feed.c.id < number),
        ))
    return session.execute(statement.order_by(
        feed.c.created_at.desc(), feed.c.kind.desc(), feed.c.id.desc(),
    ).limit(limit + 1)).all()


def _reference(kind, number, url=None):
    return {"kind": kind, "id": number, "url": url}


def _subject(session, number, fallback):
    row = session.get(ResearchSubject, number) if number else None
    return row.display_name if row else fallback


def _versions(session, kind, row):
    """Fetch only the directly preceding immutable revision when expanded."""
    if kind in {"model", "configuration"}:
        previous = session.get(_SOURCES[kind], row.supersedes_id) if row.supersedes_id else None
        return [{"title": revision.name, "version": revision.version,
                 "changed_at": _iso(revision.created_at), "summary": _text(revision.change_summary)}
                for revision in (row, previous) if revision is not None]
    if kind == "roadmap":
        snapshot = (row.payload or {}).get("forecast_snapshot") or {}
        previous_id = snapshot.get("previous_forecast_mention_id")
        previous = session.get(RoadmapMention, previous_id) if previous_id else None
        return [{"title": ((mention.payload or {}).get("forecast_snapshot") or {}).get("title") or "Forecast",
                 "changed_at": _iso(mention.created_at), "summary": _text(mention.summary)}
                for mention in (row, previous) if mention is not None]
    return []


def _change(session, kind, row, *, detail=False):
    if kind == "improvement":
        return _improvement(session, row, detail=detail)
    metadata = dict(getattr(row, "payload", None) or {})
    presentation = metadata.get("research_desk") or {}
    if not isinstance(presentation, dict):
        presentation = {}
    summary, title, subject = "", "", "Research"
    rationale = metadata.get("rationale") or metadata.get("information_gain")
    sources, details = [], []
    reference = _reference(kind, row.id)
    if kind == "write":
        summary = row.write_summary or "Research updated"
        title = summary
        subject = presentation.get("subject") or metadata.get("subject") or row.destination_type.replace("_", " ").title()
        path = _TARGET_PATHS.get(row.destination_type)
        reference = _reference(row.destination_type, row.destination_id,
                               path.format(id=row.destination_id) if path and row.destination_id else None)
        sources = row.source_document_ids_json or []
        details = [{"label": "Previous version", "value": row.before_version_id},
                   {"label": "Current version", "value": row.after_version_id}]
        if detail:
            details.extend([{"label": "Previous content", "value": _text(row.before_text, 8000)},
                            {"label": "Changes", "value": _text(row.diff_text, 12000)}])
        if row.destination_type in {"informal_research_doc", "informal_research_document"}:
            document = session.get(InformalResearchDocument, row.destination_id)
            if document:
                title, subject = document.title, document.subject or subject
                reference["url"] = f"/informal-research/{quote(document.doc_key, safe='')}"
        elif row.destination_type == "research_doc":
            report = session.get(ResearchReport, row.destination_id)
            reference["url"] = f"/reports/{quote(report.slug, safe='')}" if report else None
    elif kind == "note":
        title, summary, subject = row.title, "Note added", "Research notes"
        reference = _reference("note", row.id, f"/notes/{row.id}")
        if detail:
            details = [{"label": "Current note", "value": _text(row.body_md, 8000)}]
    elif kind == "change":
        title, summary = row.change_kind.replace("_", " ").title(), row.after_summary or row.change_kind
        subject = metadata.get("subject") or row.object_type.replace("_", " ").title()
        rationale = row.rationale
        sources = metadata.get("source_document_ids") or []
        path = _TARGET_PATHS.get(row.object_type)
        reference = _reference(row.object_type, row.object_id, path.format(id=row.object_id) if path else None)
        details = [{"label": "Previous state", "value": row.before_summary},
                   {"label": "Updated state", "value": row.after_summary}]
    elif kind == "consensus":
        subject = _subject(session, row.subject_id, row.estimate_metric or "Consensus")
        owner = metadata.get("estimate_owner") or row.source_name or "Source"
        title = f"{owner}: {row.estimate_metric or row.evidence_kind}"
        summary = row.summary or title
        sources = [row.source_document_id] if row.source_document_id else []
        reference = _reference("consensus", row.id, f"/consensus?subject_id={row.subject_id}" if row.subject_id else "/consensus")
        details = [{"label": "Period", "value": row.estimate_period},
                   {"label": "Value", "value": row.estimate_value},
                   {"label": "Unit", "value": row.estimate_unit},
                   {"label": "Prior value", "value": metadata.get("prior_estimate_value")},
                   {"label": "Source author", "value": row.author}]
    elif kind == "roadmap":
        entry = session.get(RoadmapEntry, row.entry_id)
        snapshot = metadata.get("forecast_snapshot") or {}
        title = snapshot.get("title") or (entry.title if entry else "Roadmap updated")
        subject = entry.subject if entry and entry.subject else "Roadmap"
        summary = row.summary
        reference = _reference("roadmap", row.entry_id, f"/roadmaps/{row.entry_id}")
        sources = metadata.get("source_document_ids") or ([row.source_document_id] if row.source_document_id else [])
        rationale = snapshot.get("change_rationale") or metadata.get("change_rationale") or rationale
        details = [{"label": "Change", "value": row.mention_kind},
                   {"label": "Forecast window", "value": snapshot.get("expected_window") or row.expected_window},
                   {"label": "Forecast owner", "value": snapshot.get("owner") or row.source_owner},
                   {"label": "Outcome", "value": metadata.get("outcome")},
                   {"label": "Observed window", "value": metadata.get("observed_window")},
                   {"label": "Revision", "value": snapshot.get("revision_kind")},
                   {"label": "Previous forecast", "value": snapshot.get("previous_forecast_mention_id")},
                   {"label": "Evaluated forecast", "value": metadata.get("evaluated_forecast_mention_id")}]
    elif kind in {"model", "configuration"}:
        title, summary = row.name, row.change_summary
        if kind == "model":
            subject = _subject(session, row.subject_id, row.name)
            reference = _reference(kind, row.id, f"/product-models?q={quote(row.model_key, safe='')}")
        else:
            parent = session.get(ProductModelRevision, row.model_revision_id)
            subject = _subject(session, parent.subject_id if parent else None, parent.name if parent else row.product_key)
            reference = _reference(kind, row.id, f"/product-configurations/{quote(row.configuration_key, safe='')}")
        sources = row.source_document_ids_json or []
        details = [{"label": "Version", "value": row.version},
                   {"label": "Previous revision", "value": row.supersedes_id}]
    elif kind == "extension":
        extension = session.get(ResearchExtension, row.extension_id)
        title = f"{extension.kind.title() if extension else 'Extension'} {row.action}: {extension.capability if extension else row.extension_id}"
        subject = "Research capabilities"
        receipt = dict(row.receipt_json or {})
        summary = receipt.get("summary") or receipt.get("reason") or title
        reference = _reference(kind, row.extension_id)
        candidate = session.get(ResearchExtensionVersion, row.candidate_id) if row.candidate_id else None
        manifest = dict(candidate.manifest_json or {}) if candidate else {}
        rationale = manifest.get("description") or manifest.get("purpose")
        sources = manifest.get("source_document_ids") or []
        details = [{"label": "Transition", "value": row.action},
                   {"label": "Prior active version", "value": row.prior_version_id},
                   {"label": "Resulting active version", "value": row.active_version_id}]
    elif kind == "world":
        receipt = dict(metadata.get("receipt") or {})
        summary = row.summary or "Research updated"
        title = summary
        sources = receipt.get("source_document_ids") or []
        if row.event_kind == "worldview_scenario_write":
            scenario = session.get(ResearchSubjectScenario, receipt.get("scenario_id"))
            subject = _subject(session, receipt.get("subject_id"), "World models")
            reference = _reference("world", receipt.get("scenario_id"),
                                   f"/worlds/{receipt['subject_id']}/{quote(receipt.get('world_key', ''), safe='')}")
            rationale = (scenario.audit_json or {}).get("selection_rationale") if scenario else None
            details = [{"label": "World version", "value": receipt.get("world_version")},
                       {"label": "Changed premises", "value": ", ".join(receipt.get("changed_premises") or [])}]
        elif row.event_kind == "worldview_memory_write":
            subject = "Beliefs and memory"
            segment = session.get(CloudAgentMemorySegment, receipt.get("segment_id"))
            if segment:
                title = segment.title
            reference = _reference("memory", receipt.get("segment_id"))
        else:
            subject = "Research notes"
            reference = _reference("journal", receipt.get("document_id"), "/worldview-journal")
    why = presentation.get("why_it_matters") or metadata.get("why_it_matters") or rationale
    result = {"id": f"{kind}:{row.id}", "kind": kind, "subject": _text(subject, 255),
              "title": _text(title, 255), "what_changed": _text(summary),
              "why_it_matters": _text(why) or None, "changed_at": _iso(row.created_at),
              "reference": reference, "details_available": True}
    if detail:
        ids = [number for number in sources if type(number) is int and number > 0][:30]
        evidence = session.query(Document.id, Document.title).filter(Document.id.in_(ids)).all() if ids else []
        result.update(rationale=_text(rationale, 4000) or None,
                      evidence=[{"id": number, "title": name, "url": f"/docs/{number}"} for number, name in evidence],
                      versions=_versions(session, kind, row),
                      details=[item for item in details if item["value"] is not None and item["value"] != ""])
    return result


def _improvement(session, row, *, detail=False):
    from thoth_terminal.operator_research_relay_service import get_operator_research_message

    message = get_operator_research_message(session, row.id)
    result = {"id": f"improvement:{row.id}", "kind": message["kind"],
              "subject": "Research improvements", "title": _text(message["document_title"], 255),
              "what_changed": _text(message["text"]), "why_it_matters": _text(message["rationale"]) or None,
              "changed_at": _iso(row.created_at), "reference": _reference("operator_message", row.id),
              "details_available": True}
    if not detail:
        return result
    related_id = message.get("reply_to_request_id")
    related = None
    if type(related_id) is int and session.execute(_operator_messages(
        select(ExternalAgentResearchRequest.id).where(ExternalAgentResearchRequest.id == related_id),
        outgoing=False,
    )).first():
        related = get_operator_research_message(session, related_id)
    refs = message.get("write_references") or []
    related_refs = message.get("related_write_references") or []
    versions, seen = [], set()
    for reference in refs + related_refs:
        for field, kind in (("model_revision_id", "model"), ("configuration_revision_id", "configuration")):
            number = reference.get(field)
            if type(number) is int and (kind, number) not in seen:
                revision = session.get(_SOURCES[kind], number)
                if revision is not None:
                    versions.extend(_versions(session, kind, revision))
                seen.add((kind, number))
    sources = [number for number in message["source_document_ids"] if type(number) is int and number > 0][:30]
    evidence = session.query(Document.id, Document.title).filter(Document.id.in_(sources)).all() if sources else []
    details = [{"label": "Message", "value": message["text"]},
               {"label": "Speaker", "value": message["speaker"]},
               {"label": "Attribution basis", "value": message["claim_basis"]},
               {"label": "Status", "value": message["status"]}]
    if message.get("assessment"):
        details.append({"label": "Assessment", "value": message["assessment"]})
    if related:
        details.append({"label": "Related message", "value": f"{related['request_id']}: {related['document_title']}"})
    for label, references in (("Write references", refs), ("Related write references", related_refs)):
        if references:
            details.append({"label": label, "value": json.dumps(references, ensure_ascii=False, indent=2)})
    result.update(content_md=message["text"], rationale=message["rationale"] or None,
                  speaker=message["speaker"], claim_basis=message["claim_basis"],
                  attribution_is_claimed=message["attribution_is_claimed"],
                  assessment=message.get("assessment"), assessment_is_verified=message.get("assessment_is_verified", False),
                  write_references=refs,
                  related_write_references=related_refs, related_message=related,
                  reply_to_request_id=related_id, cloud_run_id=message.get("cloud_run_id"),
                  evidence=[{"id": number, "title": title, "url": f"/docs/{number}"} for number, title in evidence],
                  versions=versions, details=details)
    return result


def get_research_change(session: Session, change_id: str, *, operator: bool = False):
    if not operator:
        raise PermissionError("Operator access is required for research change details")
    try:
        kind, number = str(change_id).split(":", 1)
        if kind not in _SOURCES or not number.isdigit() or int(number) < 1:
            raise ValueError
    except ValueError as exc:
        raise ValueError("Invalid research change ID") from exc
    if kind == "improvement":
        row = session.scalars(_operator_messages(
            select(ExternalAgentResearchRequest).where(ExternalAgentResearchRequest.id == int(number)),
            kinds=_IMPROVEMENT_KINDS,
        )).first()
    else:
        row = session.get(_SOURCES[kind], int(number))
    if row is None or (kind == "world" and row.event_kind not in _CLOUD_CHANGES) or (
        kind == "extension" and row.action not in _EXTENSION_TRANSITIONS
    ):
        raise LookupError("Research change not found")
    return _change(session, kind, row, detail=True)


def get_research_desk(session: Session, *, mode="minimal", limit=20, before_id=None, operator=False):
    if mode not in {"minimal", "operator"}:
        raise ValueError("mode must be minimal or operator")
    if mode == "operator" and not operator:
        raise PermissionError("Operator access is required for the detailed research desk")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("limit must be 1-50")
    rows = _page(session, mode, limit, before_id)
    more, rows = len(rows) > limit, rows[:limit]
    dispatches, groups = [], {}
    if mode == "minimal":
        for item in rows:
            run = session.query(CloudGoalRun.id, CloudGoalRun.final_result, CloudGoalRun.completed_at,
                                CloudGoalRun.created_at).filter_by(id=item.id).one()
            final = dict(run.final_result or {})
            dispatches.append({"id": run.id, "title": final.get("title") or "Research dispatch",
                               "content_md": final.get("dispatch_md") or "",
                               "published_at": _iso(run.completed_at or run.created_at), "url": "/agents/cloud"})
    else:
        for kind, number, _ in rows:
            query = session.query(_SOURCES[kind]).filter_by(id=number)
            if kind == "write":
                query = query.options(defer(LocalAgentWriteDiff.before_text), defer(LocalAgentWriteDiff.after_text),
                                      defer(LocalAgentWriteDiff.diff_text))
            change = _change(session, kind, query.one())
            groups.setdefault(change.pop("subject"), []).append(change)
    needs_input = []
    if mode == "operator":
        from thoth_terminal.operator_research_relay_service import get_operator_research_message
        pending = session.scalars(_operator_messages(
            select(ExternalAgentResearchRequest).where(ExternalAgentResearchRequest.status == "waiting"),
            kinds=("question", "capability_request"),
        ).order_by(ExternalAgentResearchRequest.id.asc()).limit(50)).all()
        for request in pending:
            message = get_operator_research_message(session, request.id)
            needs_input.append({"id": message["request_id"], "title": message["document_title"],
                                "text": message["text"] + ("…" if message["text_truncated"] else ""),
                                "created_at": _iso(request.created_at)})
    return {"mode": mode, "operator_available": operator, "dispatches": dispatches,
            "groups": [{"subject": subject, "changes": changes} for subject, changes in groups.items()],
            "needs_input": needs_input,
            "pagination": {"before_id": before_id, "has_more": more,
                           "next_before_id": _cursor(mode, rows[-1]) if more else None},
            "coverage": {"description": "Published research dispatches" if mode == "minimal" else
                         "Recorded research writes, consensus additions, roadmap mentions and outcomes, product revisions, notes, worlds, memory, extension transitions, and private research improvements."}}
