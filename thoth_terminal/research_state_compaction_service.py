"""Source-bound, immutable prompt projections on the existing snapshot rail.

Canonical research objects are never edited by compaction. A projection is a
model-authored summary, not proof of semantic equivalence or calculation data.
The caller owns the transaction, including the replay receipt.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, aliased

from thoth_terminal import models as m
from thoth_terminal.cloud_agent_memory_service import CLOUD_RESEARCH_HEARTBEAT_AGENT_KEY
from thoth_terminal.cloud_goal_run_service import append_cloud_goal_event
from thoth_terminal.db import utcnow
from thoth_terminal.research_state_tokens import POLICIES, budget_status, count_tokens, token_page
from thoth_terminal.worldview_write_service import _authorized_run


COMPILER_VERSION = "research-state-compaction-v1"
EVENT_KIND = "research_state_compaction"
_MODELS = {
    "belief": m.CloudAgentMemorySegment,
    "belief_history": m.CloudAgentMemoryEvent,
    "world": m.ResearchSubjectScenario,
    "research_skill": m.ResearchSkill,
    "extension": m.ResearchExtensionVersion,
    "journal": m.InformalResearchDocument,
    "research_report": m.ResearchReportVersion,
    "knowledge_asset": m.KnowledgeAsset,
    "product_model": m.ProductModelRevision,
    "product_configuration": m.ProductConfigurationRevision,
    "agentx_configuration": m.DatasetVersion,
    "dataset": m.DatasetVersion,
    "supply_chain": m.SupplyChain,
    "roadmap": m.RoadmapEntry,
    "operator_message": m.ExternalAgentResearchRequest,
    "stable_state": m.CloudPromptStateSnapshot,
    "volatile_state": m.CloudPromptStateSnapshot,
}
_SOURCE_FIELDS = {"source_document_id", "source_document_ids", "source_document_ids_json",
                  "source_url", "source_refs", "source_references", "evidence_links_json",
                  "evidence_bundle", "document_id", "entity_refs_json"}
_CONDITION_FIELDS = {"premises_json", "assumptions_json", "invalidation_triggers_json",
                     "open_questions_json", "dependencies", "conditions", "retained_conditions",
                     "uncertainties", "uncertainty", "premises", "premise_changes"}
_PROTECTED_FIELDS = {"key_metrics_json", "variables_json", "outputs_json", "bridges_json",
                     "inputs_json", "model_revision_id", "scoring_json", "trigger_json",
                     "execution", "permissions", "system_prompt", "code", "schema", "nodes", "edges"}


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _plain(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _record(row: Any) -> dict[str, Any]:
    # Access and accounting are operational, not revisions to research content.
    ignored = {"last_accessed_at", "compressed_at", "token_estimate", "token_estimates", "access_count"}
    return {column.key: _plain(getattr(row, column.key)) for column in row.__table__.columns if column.key not in ignored}


def _latest(q: Any, model: Any, group_fields: tuple[str, ...]) -> Any:
    """Filter before pagination, so old revisions cannot hide current objects."""
    newer = aliased(model)
    same = [getattr(newer, field) == getattr(model, field) for field in group_fields]
    rank = or_(newer.version > model.version,
               and_(newer.version == model.version, newer.id > model.id))
    return q.filter(~q.session.query(newer.id).filter(*same, rank).exists())


def _query(session: Session, kind: str, *, source_read: bool = False, history_read: bool = False) -> Any:
    if kind not in _MODELS:
        raise ValueError("Unknown or unsupported research state kind")
    model = _MODELS[kind]
    q = session.query(model)
    if kind == "belief":
        q = q.filter_by(agent_key=CLOUD_RESEARCH_HEARTBEAT_AGENT_KEY)
        if not source_read:
            q = q.filter_by(status="active").filter(~model.segment_type.in_(["identity", "operating_principle"]))
    elif kind == "belief_history":
        q = q.filter_by(agent_key=CLOUD_RESEARCH_HEARTBEAT_AGENT_KEY)
    elif kind == "world":
        newer = aliased(model)
        key = func.coalesce(model.audit_json["world_key"].as_string(), model.slug)
        other_key = func.coalesce(newer.audit_json["world_key"].as_string(), newer.slug)
        version = func.coalesce(model.audit_json["world_version"].as_integer(), 0)
        other_version = func.coalesce(newer.audit_json["world_version"].as_integer(), 0)
        if not history_read:
            q = q.filter(~session.query(newer.id).filter(
                newer.subject_id == model.subject_id, other_key == key,
                or_(other_version > version, and_(other_version == version, newer.id > model.id)),
            ).exists())
    elif kind == "extension":
        q = q.join(m.ResearchExtension, m.ResearchExtension.id == model.extension_id)
        if not history_read:
            q = q.filter(m.ResearchExtension.active_version_id == model.id)
    elif kind in {"product_model", "product_configuration"}:
        if not history_read:
            q = _latest(q, model, ("model_key" if kind == "product_model" else "configuration_key",))
    elif kind in {"dataset", "agentx_configuration"}:
        q = q.join(m.Dataset, m.Dataset.id == model.dataset_id).filter(model.status == "published")
        newer = aliased(model)
        if not history_read:
            q = q.filter(~session.query(newer.id).filter(
                newer.dataset_id == model.dataset_id, newer.status == "published",
                or_(newer.version > model.version, and_(newer.version == model.version, newer.id > model.id)),
            ).exists())
        q = q.filter(m.Dataset.source_kind == "agentx_configuration" if kind == "agentx_configuration"
                     else or_(m.Dataset.source_kind != "agentx_configuration", m.Dataset.source_kind.is_(None)))
    elif kind == "research_report":
        q = q.join(m.ResearchReport, m.ResearchReport.id == model.report_id)
        newer = aliased(model)
        if not history_read:
            q = q.filter(or_(
                m.ResearchReport.latest_published_version_id == model.id,
                and_(m.ResearchReport.latest_published_version_id.is_(None),
                     ~session.query(newer.id).filter(newer.report_id == model.report_id,
                         or_(newer.version > model.version,
                             and_(newer.version == model.version, newer.id > model.id))).exists()),
            ))
    elif kind == "operator_message":
        q = q.join(m.ExternalAgentPrincipal, m.ExternalAgentPrincipal.id == model.principal_id)
        q = q.join(m.Document, m.Document.id == model.document_id).filter(
            m.ExternalAgentPrincipal.slug == "owner-agent", m.ExternalAgentPrincipal.status == "active",
            m.Document.extra["rights_class"].as_string() == "private",
            m.Document.extra["access_scope"].as_string() == "operator",
            m.Document.extra["owner_principal_id"].as_integer() == model.principal_id,
            m.Document.extra["acquisition_mode"].as_string() == "operator_conversation",
            m.Document.extra["operator_research_relay"].is_not(None),
        )
    elif kind in {"stable_state", "volatile_state"}:
        from thoth_terminal.cloud_prompt_state_service import CLOUD_PROMPT_STATE_COMPILER_VERSION
        from thoth_terminal.cloud_prompt_volatile_state_service import CLOUD_PROMPT_VOLATILE_COMPILER_VERSION
        compiler = CLOUD_PROMPT_STATE_COMPILER_VERSION if kind == "stable_state" else CLOUD_PROMPT_VOLATILE_COMPILER_VERSION
        newer = aliased(model)
        q = q.filter(model.compiler_version == compiler)
        if not history_read:
            q = q.filter(model.status == "active").filter(~session.query(newer.id).filter(
                newer.agent_key == model.agent_key, newer.compiler_version == compiler, newer.status == "active",
                or_(newer.created_at > model.created_at,
                    and_(newer.created_at == model.created_at, newer.id > model.id)),
            ).exists())
    elif hasattr(model, "status"):
        q = q.filter(~model.status.in_(["archived", "superseded", "deleted"]))
    return q


def _annotations(value: Any, path: str = "") -> tuple[dict, dict, list[str]]:
    references, conditions, protected = {}, {}, []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if key in _SOURCE_FIELDS and child:
                references[child_path] = child
            if key in _CONDITION_FIELDS and child:
                conditions[child_path] = child
            if key in _PROTECTED_FIELDS and child:
                protected.append(child_path)
            refs, conds, fields = _annotations(child, child_path)
            references.update(refs)
            conditions.update(conds)
            protected.extend(fields)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            refs, conds, fields = _annotations(child, f"{path}[{index}]")
            references.update(refs)
            conditions.update(conds)
            protected.extend(fields)
    return references, conditions, protected


def _source(session: Session, kind: str, object_id: int, *, _row: Any = None, lock: bool = False,
            allow_history: bool = False) -> dict[str, Any]:
    if type(object_id) is not int or object_id < 1:
        raise ValueError("object_id must be a positive integer")
    model = _MODELS.get(kind)
    if model is None:
        raise ValueError("Unknown or unsupported research state kind")
    query = _query(session, kind, source_read=True, history_read=allow_history).filter(model.id == object_id).populate_existing()
    row = _row if _row is not None else (query.with_for_update(of=model) if lock else query).first()
    if row is None:
        raise ValueError("Research state source is not current or is outside the permitted scope")
    current = not allow_history or _query(session, kind, source_read=True).filter(model.id == object_id).count() == 1
    data = _record(row)
    locator = {"kind": kind, "object_id": object_id, "table": model.__tablename__, "version_id": object_id}
    executable = False
    if kind == "extension":
        extension = session.query(m.ResearchExtension).filter_by(id=row.extension_id).populate_existing()
        extension = (extension.with_for_update() if lock else extension).one()
        if not allow_history and extension.active_version_id != row.id:
            raise ValueError("Extension source is not the current adopted version")
        locator.update(parent_id=row.extension_id, version=row.version, capability=extension.capability)
        data["extension"] = _record(extension)
        executable = bool((row.manifest_json or {}).get("execution"))
        from thoth_terminal.research_extension_service import _assert_version_integrity
        _assert_version_integrity(session, row)
    elif kind in {"dataset", "agentx_configuration"}:
        data["dataset"] = _record(session.get(m.Dataset, row.dataset_id))
        data["schema"] = [_record(column) for column in session.query(m.DatasetColumn)
                          .filter_by(dataset_version_id=row.id).order_by(m.DatasetColumn.sort_order, m.DatasetColumn.id)]
        data["row_count"] = session.query(m.DatasetRow.id).filter_by(dataset_version_id=row.id).count()
        locator.update(parent_id=row.dataset_id, version=row.version, rows_included=False)
    elif kind == "research_report":
        data["report"] = _record(session.get(m.ResearchReport, row.report_id))
        locator.update(parent_id=row.report_id, version=row.version)
    elif kind == "knowledge_asset":
        version = session.get(m.KnowledgeAssetVersion, row.latest_published_version_id) if row.latest_published_version_id else (
            session.query(m.KnowledgeAssetVersion).filter_by(asset_id=row.id)
            .order_by(m.KnowledgeAssetVersion.version.desc(), m.KnowledgeAssetVersion.id.desc()).first())
        if version is not None:
            if version.asset_id != row.id:
                raise ValueError("Knowledge asset version does not belong to source")
            data["version"] = _record(version)
            locator.update(version_id=version.id, version=version.version)
    elif kind == "supply_chain":
        snapshot = session.query(m.SupplyChainSnapshot).filter_by(chain_id=row.id).order_by(
            m.SupplyChainSnapshot.version.desc(), m.SupplyChainSnapshot.updated_at.desc(), m.SupplyChainSnapshot.id.desc()).first()
        if snapshot is not None:
            data["snapshot"] = _record(snapshot)
            data["nodes"] = [_record(node) for node in session.query(m.SupplyChainNode)
                             .filter_by(snapshot_id=snapshot.id).order_by(m.SupplyChainNode.id)]
            data["edges"] = [_record(edge) for edge in session.query(m.SupplyChainEdge)
                             .filter_by(snapshot_id=snapshot.id).order_by(m.SupplyChainEdge.id)]
            data["metric_bindings"] = [_record(binding) for binding in session.query(m.SupplyChainMetricBinding)
                                      .filter_by(snapshot_id=snapshot.id).order_by(m.SupplyChainMetricBinding.id)]
            locator.update(version_id=snapshot.id, version=snapshot.version)
        version = session.query(m.SupplyChainVersion).filter_by(chain_id=row.id).order_by(
            m.SupplyChainVersion.version.desc(), m.SupplyChainVersion.id.desc()).first()
        if version is not None:
            data["graph_version"] = _record(version)
    elif kind == "operator_message":
        document = session.get(m.Document, row.document_id)
        metadata = dict((document.extra or {}).get("operator_research_relay") or {})
        if not metadata:
            raise ValueError("Operator source is not an authored owner relay message")
        data = {"request": data, "text": document.raw_text, "relay": metadata,
                "document_id": document.id, "rights_class": "private", "access_scope": "operator"}
        locator.update(document_id=document.id, principal_id=row.principal_id,
                       rights_class="private", access_scope="operator")
    elif kind in {"stable_state", "volatile_state"}:
        if hashlib.sha256(row.content_md.encode()).hexdigest() != row.content_sha256:
            raise ValueError("Compiled source content hash is invalid")
        locator.update(compiler_version=row.compiler_version, agent_key=row.agent_key)
    elif hasattr(row, "version"):
        locator["version"] = row.version
    if kind == "world":
        locator.update(world_key=(row.audit_json or {}).get("world_key", row.slug),
                       version=(row.audit_json or {}).get("world_version", 0), parent_id=row.subject_id)
    content = _json(data)
    source_hash = hashlib.sha256(content.encode()).hexdigest()
    refs, conditions, fields = _annotations(data)
    refs["original"] = locator
    budget = budget_status(kind, content)
    mode = "protected" if executable else POLICIES[kind].mode
    if not current and mode == "compact":
        mode = "page"
    if kind == "belief" and (row.status != "active" or row.segment_type in {"identity", "operating_principle"}):
        mode = "protected" if row.segment_type in {"identity", "operating_principle"} else "page"
    if mode != "compact":
        budget = {**budget, "mode": mode, "compaction_due": False}
    text_fields = {key: value for key, value in data.items() if isinstance(value, str)}
    if kind == "belief_history":
        historical_current = (row.payload_json or {}).get("current")
        if isinstance(historical_current, dict) and isinstance(historical_current.get("content_md"), str):
            text_fields["current.content_md"] = historical_current["content_md"]
    display_text_field = None
    if kind in {"belief", "belief_history", "extension", "stable_state", "volatile_state"}:
        text_fields["content_md"] = row.content_md or ""
        display_text_field = "content_md"
    elif kind == "research_skill":
        from thoth_terminal.cloud_skill_runtime_service import _instruction_text
        # Use the owning runtime's instructions_md/system_prompt/prompt priority.
        text_fields["instructions_md"] = _instruction_text(row)
        display_text_field = "instructions_md"
    return {"kind": kind, "object_id": object_id, "source_hash": source_hash,
            "source_location": locator, "version_identity": locator, "budget": budget,
            "mode": mode, "current_source": current, "content": content,
            "text_fields": text_fields, "display_text_field": display_text_field,
            "source_refs": refs,
            "retained_conditions": [f"{path}: {_json(value)}" for path, value in sorted(conditions.items())],
            "protected_fields": sorted(set(fields)),
            "conditions_preservation": "Exact structured conditions retained as metadata; semantic preservation is unverified."}


def _snapshot_key(source: dict, summary_hash: str | None = None) -> str:
    base = f"{COMPILER_VERSION}:{source['kind']}:{source['object_id']}:{source['source_hash']}"
    return f"{base}:{summary_hash}" if summary_hash is not None else base


def _active_budget(source: dict, projection: dict | None) -> dict[str, Any]:
    budget = budget_status(source["kind"], projection["summary_md"]) if projection else source["budget"]
    return {**budget, "mode": source["mode"],
            "compaction_due": budget["compaction_due"] and source["mode"] == "compact"}


def _latest_snapshots(session: Session, sources: list[dict]) -> dict[str, Any]:
    """Return one newest snapshot per source in one bounded database read.

    Window ranking stays in SQL so historical summaries are not materialized
    into the active renderer when the same source has many improvements.
    """
    sources = [source for source in sources if source["mode"] == "compact"]
    if not sources:
        return {}
    model = m.CloudPromptStateSnapshot
    ranked = session.query(
        model.id.label("snapshot_id"),
        func.row_number().over(
            partition_by=(model.manifest_json["kind"].as_string(),
                          model.manifest_json["object_id"].as_integer(),
                          model.manifest_json["source_hash"].as_string()),
            order_by=(model.created_at.desc(), model.id.desc()),
        ).label("rank"),
    ).filter(model.compiler_version == COMPILER_VERSION, model.status == "active",
             model.agent_key == CLOUD_RESEARCH_HEARTBEAT_AGENT_KEY,
             model.manifest_json["source_hash"].as_string().in_([source["source_hash"] for source in sources]),
             model.manifest_json["kind"].as_string().in_({source["kind"] for source in sources}),
             model.manifest_json["object_id"].as_integer().in_({source["object_id"] for source in sources}),
    ).subquery()
    rows = session.query(model).join(ranked, ranked.c.snapshot_id == model.id).filter(ranked.c.rank == 1).all()
    return {_snapshot_key(row.manifest_json): row for row in rows}


def _public_annotations(source: dict) -> dict[str, Any]:
    """Bound read payloads while keeping exact annotations in immutable storage."""
    result = {}
    pages = {}
    for field in ("source_refs", "retained_conditions", "protected_fields"):
        value = source[field]
        encoded = _json(value)
        if count_tokens(encoded) <= 500:
            result[field] = value
            continue
        pages[field] = {**token_page(encoded, limit_tokens=500),
                        "sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                        "retrieval": "Retrieve full annotations from canonical source pages."}
        result[field] = ({"original": source["source_location"], "annotations_truncated": True}
                         if field == "source_refs" else [])
    if pages:
        result["annotation_pages"] = pages
        result["annotations_truncated"] = True
    return result


def _projection(session: Session, source: dict, snapshots: dict[str, Any] | None = None) -> dict | None:
    row = (snapshots if snapshots is not None else _latest_snapshots(session, [source])).get(_snapshot_key(source))
    if row is None:
        return None
    metadata = dict(row.manifest_json or {})
    if metadata.get("source_hash") != source["source_hash"] or metadata.get("source_location") != source["source_location"]:
        return None
    if hashlib.sha256(row.content_md.encode()).hexdigest() != row.content_sha256:
        return None
    public_metadata = {key: value for key, value in metadata.items()
                       if key not in {"source_refs", "retained_conditions", "protected_fields"}}
    public_metadata.update(_public_annotations(source))
    return {"snapshot_id": row.id, "summary_md": row.content_md,
            "summary_tokens": count_tokens(row.content_md), "metadata": public_metadata,
            "semantic_preservation_verified": False}


def get_research_state_budget_policy() -> dict[str, Any]:
    return {"kinds": {kind: {"ceiling_tokens": policy.ceiling_tokens,
                             "target_tokens": policy.target_tokens, "mode": policy.mode,
                             "persistent": kind in _MODELS}
                      for kind, policy in POLICIES.items()},
            "source_policy": "Canonical source remains unchanged; summaries are derived projections.",
            "compiler_version": COMPILER_VERSION}


def list_state_compaction_candidates(session: Session, kinds: list[str] | None = None,
                                     after_id: int = 0, limit: int = 20) -> dict[str, Any]:
    """Page the current-object catalog; after_id is an ordinal, not a table ID.

    IDs overlap different tables. This offset cursor traverses sorted kinds and
    ascending source IDs. The returned total is a SQL count, not a sampled count.
    Each page inspects only limit source objects; concurrent insertions can move
    ordinal positions, so restart an inventory after a catalog-changing write.
    """
    if type(after_id) is not int or after_id < 0:
        raise ValueError("after_id must be a nonnegative catalog cursor")
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("limit must be 1-50")
    selected = sorted(set(kinds if kinds is not None else _MODELS))
    if not selected or any(kind not in _MODELS for kind in selected):
        raise ValueError("kinds must contain supported persistent state kinds")
    counts = {kind: _query(session, kind).count() for kind in selected}
    total = sum(counts.values())
    skip, remaining, items, sources = after_id, limit, [], []
    for kind in selected:
        if skip >= counts[kind]:
            skip -= counts[kind]
            continue
        ids = _query(session, kind).with_entities(_MODELS[kind].id).order_by(_MODELS[kind].id).offset(skip).limit(remaining).all()
        skip = 0
        for (object_id,) in ids:
            source = _source(session, kind, int(object_id))
            sources.append(source)
        remaining -= len(ids)
        if not remaining:
            break
    snapshots = _latest_snapshots(session, sources)
    for source in sources:
        projection = _projection(session, source, snapshots)
        items.append({key: source[key] for key in ("kind", "object_id", "source_hash", "source_location", "mode")}
                     | {"budget": _active_budget(source, projection), "source_budget": source["budget"],
                        "source_compaction_due": source["budget"]["compaction_due"],
                        "compacted_projection": {"snapshot_id": projection["snapshot_id"],
                                                 "summary_tokens": projection["summary_tokens"]} if projection else None})
    next_cursor = after_id + len(items)
    more = next_cursor < total
    return {"items": items, "candidates": items, "count": len(items), "total_count": total,
            "counts_by_kind": counts, "after_id": after_id,
            "next_after_id": next_cursor if more else None, "has_more": more, "truncated": more,
            "cursor_kind": "current_object_catalog_offset"}


def get_state_compaction_source(session: Session, kind: str, object_id: int,
                                offset_tokens: int = 0, limit_tokens: int = 6000,
                                content_field: str | None = None) -> dict[str, Any]:
    source = _source(session, kind, object_id, allow_history=True)
    content = source.pop("content")
    fields = source.pop("text_fields")
    if content_field is not None:
        if content_field not in fields:
            raise ValueError("content_field must identify an existing source text field")
        content = fields[content_field]
    source["source_page"] = token_page(content, offset_tokens=offset_tokens, limit_tokens=limit_tokens)
    source["source_page"]["content_field"] = content_field
    source["compacted_projection"] = _projection(session, source) if source["mode"] == "compact" else None
    source["source_budget"] = source["budget"]
    source["budget"] = _active_budget(source, source["compacted_projection"])
    source["truncated"] = source["source_page"]["truncated"]
    source.update(_public_annotations(source))
    return source


def compact_research_state(session: Session, *, cloud_run_id: int, capability: str,
                           operation_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Save one immutable derived projection and its replay-safe run receipt."""
    if not isinstance(operation_key, str) or not 1 <= len(operation_key.strip()) <= 120:
        raise ValueError("operation_key must be 1-120 characters")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    allowed_fields = {"kind", "object_id", "expected_source_hash", "summary_md", "change_summary",
                      "retained_conditions", "uncertainties", "source_refs", "version_identity", "source_location"}
    if set(payload) - allowed_fields:
        raise ValueError("Unknown compaction payload fields")
    uncertainties = payload.get("uncertainties", [])
    if not isinstance(uncertainties, list) or any(not isinstance(item, str) for item in uncertainties) or count_tokens(uncertainties) > 500:
        raise ValueError("uncertainties must be a list of strings within 500 tokens")
    run = _authorized_run(session, cloud_run_id=cloud_run_id, capability=capability)
    request_hash = hashlib.sha256(_json(payload).encode()).hexdigest()
    prior = session.query(m.CloudGoalEvent).filter(
        m.CloudGoalEvent.run_id == run.id, m.CloudGoalEvent.event_kind == EVENT_KIND,
        m.CloudGoalEvent.payload["operation_key"].as_string() == operation_key,
    ).first()
    if prior is not None:
        if (prior.payload or {}).get("request_hash") != request_hash:
            raise ValueError("operation_key was already used for a different compaction")
        return {**prior.payload["receipt"], "replayed": True}
    source = _source(session, payload.get("kind"), payload.get("object_id"), lock=True)
    if source["mode"] != "compact":
        raise ValueError("Protected or paged source cannot be compacted")
    if payload.get("expected_source_hash") != source["source_hash"]:
        raise ValueError("Stale source hash; fetch the current source before compaction")
    for field in ("version_identity", "source_location"):
        if field in payload and payload[field] != source[field]:
            raise ValueError("Compaction version identity does not match current source")
    summary = payload.get("summary_md")
    change = payload.get("change_summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary_md must be nonempty text")
    if not isinstance(change, str) or not change.strip() or len(change) > 4000:
        raise ValueError("change_summary must be 1-4000 characters")
    summary = summary.strip()
    summary_tokens = count_tokens(summary)
    if summary_tokens > POLICIES[source["kind"]].target_tokens:
        raise ValueError("Summary exceeds the target token budget")
    if summary_tokens >= source["budget"]["tokens"]:
        raise ValueError("Summary must reduce source tokens")
    for field in ("retained_conditions", "source_refs"):
        if field in payload and payload[field] != source[field]:
            raise ValueError(f"{field} must retain the exact structured source values")
    # Recheck current-version membership after locks and validation. Immutable
    # superseded projections remain inert because every read validates current
    # membership and the canonical hash again.
    if _source(session, source["kind"], source["object_id"], lock=True)["source_hash"] != source["source_hash"]:
        raise ValueError("Source changed during compaction")
    # These annotations come from canonical data, never from model assertions.
    now = utcnow()
    metadata = {key: source[key] for key in ("kind", "object_id", "source_hash", "source_location",
                "version_identity", "source_refs", "retained_conditions", "protected_fields")}
    metadata.update(original_tokens=source["budget"]["tokens"], summary_tokens=summary_tokens,
                    budget=source["budget"], change_summary=change.strip(), cloud_run_id=run.id,
                    operation_key=operation_key, semantic_preservation_verified=False,
                    uncertainties=uncertainties,
                    source_preserved=True, derived_projection=True)
    summary_hash = hashlib.sha256(summary.encode()).hexdigest()
    existing = session.query(m.CloudPromptStateSnapshot).filter_by(snapshot_key=_snapshot_key(source, summary_hash)).first()
    if existing is not None:
        if (existing.content_md != summary or existing.content_sha256 != summary_hash
                or existing.manifest_json.get("source_hash") != source["source_hash"]
                or existing.manifest_json.get("source_location") != source["source_location"]):
            raise ValueError("Existing immutable compaction failed its source or summary integrity check")
        snapshot = existing
    else:
        snapshot = m.CloudPromptStateSnapshot(
            snapshot_key=_snapshot_key(source, summary_hash), agent_key=CLOUD_RESEARCH_HEARTBEAT_AGENT_KEY,
            compiler_version=COMPILER_VERSION, epoch_start=now, epoch_end=now, status="active",
            content_sha256=summary_hash, content_md=summary,
            manifest_json=metadata, created_at=now, updated_at=now)
        session.add(snapshot)
        session.flush()
    receipt = {"cloud_run_id": run.id, "operation_key": operation_key, "snapshot_id": snapshot.id,
               "kind": source["kind"], "object_id": source["object_id"],
               "source_hash": source["source_hash"], "source_location": source["source_location"],
               "original_tokens": source["budget"]["tokens"], "summary_tokens": summary_tokens,
               "source_preserved": True, "semantic_preservation_verified": False, "replayed": False}
    append_cloud_goal_event(session, run, event_kind=EVENT_KIND, summary=change,
                           payload={"operation_key": operation_key, "request_hash": request_hash, "receipt": receipt})
    return receipt


def render_compacted_state(session: Session, kind: str, object_id: int,
                           source_content: str | None = None) -> dict[str, Any]:
    """Render only a CURRENT hash-matching projection or an explicit token page.

    source_content can supply the existing caller's display format. It cannot
    choose a hash or override cache validation against the canonical object.
    """
    source = _source(session, kind, object_id)
    return _render(session, source, source_content)


def _render(session: Session, source: dict, source_content: str | None,
            snapshots: dict[str, Any] | None = None) -> dict[str, Any]:
    projection = _projection(session, source, snapshots) if source["mode"] == "compact" else None
    if projection is not None:
        return ({key: source[key] for key in ("source_hash", "source_location", "version_identity", "budget", "mode")}
               | {"content_md": projection["summary_md"], "summary_md": projection["summary_md"],
                  "budget": _active_budget(source, projection), "source_budget": source["budget"],
                  "projection": projection, "compacted_projection": projection, "compacted": True,
                  **_public_annotations(source), "truncated": False,
                  "semantic_preservation_verified": False})
    display_field = source.get("display_text_field") if source_content is not None else None
    # A supplied text string may have been captured before the canonical row
    # refresh. Known text displays always derive from that refreshed row.
    content = (source["text_fields"][display_field] if display_field else
               source["content"] if source_content is None else source_content)
    policy = POLICIES[source["kind"]]
    page = token_page(content, limit_tokens=policy.ceiling_tokens, max_limit=policy.ceiling_tokens)
    page["content_field"] = display_field or (next((key for key, value in source["text_fields"].items() if source_content == value), None) if source_content is not None else None)
    due = source["budget"]["compaction_due"] and source["mode"] == "compact"
    return ({key: source[key] for key in ("source_hash", "source_location", "version_identity", "budget", "mode")}
           | {"content_md": page["text"], "projection": None, "compacted": False, "source_budget": source["budget"],
              "source_page": page, **_public_annotations(source),
              "truncated": page["truncated"], "compaction_due": due,
              "notice": "Compaction due; retrieve remaining source pages." if due else
                        "Source excerpt truncated; retrieve remaining source pages." if page["truncated"] else None})


def render_compacted_states(session: Session, kind: str, rows: list[Any],
                            source_contents: dict[int, str] | None = None) -> dict[int, dict[str, Any]]:
    """Batch a caller's scoped rows with one current-ID and one snapshot read.

    Source adapters with child graphs still load those graphs. Beliefs, worlds
    and research skills are built from already loaded canonical rows.
    """
    if kind not in _MODELS:
        raise ValueError("Unknown or unsupported research state kind")
    ids = [int(row.id) for row in rows]
    if not ids:
        return {}
    model = _MODELS[kind]
    # Refresh in one query: caller rows can be identity-map objects loaded before
    # another transaction changed substantive content. A stale in-memory hash
    # must never admit an old projection as current.
    current = {row.id: row for row in _query(session, kind, source_read=True)
               .filter(model.id.in_(ids)).populate_existing().all()}
    sources = [_source(session, kind, object_id, _row=current[object_id]) for object_id in ids if object_id in current]
    snapshots = _latest_snapshots(session, sources)
    return {source["object_id"]: _render(session, source,
            (source_contents or {}).get(source["object_id"]), snapshots) for source in sources}
