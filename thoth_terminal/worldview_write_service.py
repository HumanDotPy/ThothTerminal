"""Run-scoped, replay-safe durable writes from a scheduled Harness turn."""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, aliased

from thoth_terminal.cloud_goal_run_service import CLOUD_GOAL_RUNNING, append_cloud_goal_event
from thoth_terminal.db import utcnow
from thoth_terminal.decision_spine_service import serialize_analyst_evidence, upsert_analyst_evidence
from thoth_terminal.external_agent_intake_service import resolve_evidence_request, worldview_resolution_key
from thoth_terminal.informal_research_service import upsert_informal_research_doc
from thoth_terminal.local_agent_service import record_action, write_research_destination
from thoth_terminal.models import AnalystEvidence, CloudGoalEvent, CloudGoalRun, Document, ResearchSubject, ResearchSubjectScenario
from thoth_terminal.roadmap_service import upsert_roadmap_update
from thoth_terminal.research_state_tokens import POLICIES, budget_status, count_tokens, token_page


def issue_worldview_write_token(run: Any) -> str:
    """Replace the capability on each attempt; store only its hash on the run."""
    token = secrets.token_urlsafe(32)
    stats = dict(run.stats_json or {})
    stats["worldview_write_capability_sha256"] = hashlib.sha256(token.encode()).hexdigest()
    run.stats_json = stats
    return token


def _authorized_run(session: Session, *, cloud_run_id: int, capability: str) -> CloudGoalRun:
    run = session.query(CloudGoalRun).filter_by(id=int(cloud_run_id)).with_for_update().first()
    if run is None or run.task_kind != "cloud_research_heartbeat" or run.status != CLOUD_GOAL_RUNNING:
        raise ValueError("Scheduled worldview run is not active")
    expected = str(dict(run.stats_json or {}).get("worldview_write_capability_sha256") or "")
    supplied = hashlib.sha256(str(capability).encode()).hexdigest()
    if not expected or not secrets.compare_digest(expected, supplied):
        raise ValueError("Invalid worldview write capability")
    return run


def _replayed_receipt(session: Session, *, cloud_run_id: int, event_kind: str, operation_key: str) -> dict[str, Any] | None:
    prior = (
        session.query(CloudGoalEvent)
        .filter(CloudGoalEvent.run_id == int(cloud_run_id), CloudGoalEvent.event_kind == event_kind)
        .all()
    )
    for event in prior:
        if str((event.payload or {}).get("operation_key") or "") == operation_key:
            return {**dict((event.payload or {}).get("receipt") or {}), "replayed": True}
    return None


def apply_worldview_write(
    session: Session,
    *,
    cloud_run_id: int,
    capability: str,
    operation_key: str,
    destination: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Write one research destination and return its versioned receipt.

    The same operation key replays the first receipt, including across a
    Harness retry. A failure leaves no successful event and can be corrected
    within the same turn.
    """
    if not 1 <= len(operation_key.strip()) <= 120:
        raise ValueError("operation_key must be 1-120 characters")
    if not isinstance(destination, dict) or not isinstance(payload, dict):
        raise ValueError("destination and payload must be objects")
    run = _authorized_run(session, cloud_run_id=cloud_run_id, capability=capability)
    replay = _replayed_receipt(session, cloud_run_id=cloud_run_id, event_kind="worldview_write", operation_key=operation_key)
    if replay is not None:
        return replay
    write_payload = dict(payload)
    metadata = dict(write_payload.get("payload") or {}) if isinstance(write_payload.get("payload"), dict) else {}
    metadata.update(cloud_goal_run_id=int(cloud_run_id), worldview_operation_key=operation_key)
    write_payload["payload"] = metadata
    action = record_action(
        session,
        run_id=None,
        action_kind="worldview_research_write",
        status="running",
        tool_name="apply_worldview_write",
        endpoint="thoth_mcp",
        request_json={"cloud_goal_run_id": int(cloud_run_id), "operation_key": operation_key, "destination": destination},
        summary=str(write_payload.get("write_summary") or "")[:500] or None,
    )
    diff = write_research_destination(
        session, destination=destination, payload=write_payload,
        run_id=None, action_id=int(action.id),
    )
    receipt = {
        "cloud_run_id": int(cloud_run_id),
        "operation_key": operation_key,
        "write_diff_id": int(diff.id),
        "destination_type": diff.destination_type,
        "destination_id": diff.destination_id,
        "after_version_id": diff.after_version_id,
        "write_summary": diff.write_summary,
        "replayed": False,
    }
    action.status = "success"
    action.destination_type = diff.destination_type
    action.destination_id = diff.destination_id
    action.summary = diff.write_summary
    action.confidence = diff.confidence
    action.response_json = receipt
    append_cloud_goal_event(
        session, run, event_kind="worldview_write",
        summary=diff.write_summary, payload={"operation_key": operation_key, "receipt": receipt},
    )
    return receipt


def _world_key(value: object) -> str:
    key = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    if not 3 <= len(key) <= 80:
        raise ValueError("world_key must contain 3-80 letters, numbers, or separators")
    return key


def _premise_key(value: object) -> str:
    key = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    if not 1 <= len(key) <= 80:
        raise ValueError("Premise key must contain 1-80 letters, numbers, or separators")
    return key


def _latest_world(session: Session, *, subject_id: int, world_key: str) -> ResearchSubjectScenario | None:
    model = ResearchSubjectScenario
    return session.query(model).filter(model.subject_id == subject_id,
        func.coalesce(model.audit_json["world_key"].as_string(), model.slug) == world_key)\
        .order_by(func.coalesce(model.audit_json["world_version"].as_integer(), 0).desc(), model.id.desc()).first()


def list_worldview_worlds(
    session: Session, *, subject_id: int | None = None, query: str = "", limit: int = 40,
    include_history: bool = False,
) -> dict[str, Any]:
    """Compact current worlds, with optional immutable prior scenario versions."""
    model = ResearchSubjectScenario
    newer = aliased(model)
    key = func.coalesce(model.audit_json["world_key"].as_string(), model.slug)
    other_key = func.coalesce(newer.audit_json["world_key"].as_string(), newer.slug)
    version = func.coalesce(model.audit_json["world_version"].as_integer(), 0)
    other_version = func.coalesce(newer.audit_json["world_version"].as_integer(), 0)
    current = ~session.query(newer.id).filter(newer.subject_id == model.subject_id, other_key == key,
        or_(other_version > version, and_(other_version == version, newer.id > model.id))).exists()
    q = session.query(model, ResearchSubject).outerjoin(ResearchSubject, ResearchSubject.id == model.subject_id).filter(current)
    if subject_id is not None:
        q = q.filter(model.subject_id == int(subject_id))
    if query.strip():
        needle = "%" + query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        q = q.filter(or_(*(field.ilike(needle, escape="\\") for field in (
            ResearchSubject.display_name, ResearchSubject.slug, key, model.name, model.thesis_summary))))
    bounded_limit = max(1, min(int(limit), 100))
    rows = q.order_by(model.updated_at.desc(), model.id.desc()).limit(bounded_limit + 1).all()
    from thoth_terminal.research_canon_service import serialize_scenario
    from thoth_terminal.research_state_compaction_service import render_compacted_states

    out = []
    omitted = 0
    projections = render_compacted_states(session, "world", [row for row, _ in rows[:bounded_limit]])
    selected_keys = [(int(row.subject_id), str((row.audit_json or {}).get("world_key") or row.slug or ""))
                     for row, _ in rows[:bounded_limit]]
    selected_scope = or_(*(and_(model.subject_id == subject, key == world_key)
                          for subject, world_key in selected_keys)) if selected_keys else model.id < 0
    version_counts = {(int(subject), str(world_key)): int(total) for subject, world_key, total in
                      session.query(model.subject_id, key, func.count(model.id)).filter(selected_scope)
                      .group_by(model.subject_id, key).all()}
    histories: dict[tuple[int, str], list[ResearchSubjectScenario]] = {}
    if include_history and selected_keys:
        ranked = session.query(model.id.label("scenario_id"),
            func.row_number().over(partition_by=(model.subject_id, key),
                order_by=(version.desc(), model.id.desc())).label("position"))\
            .filter(selected_scope).subquery()
        for historical in session.query(model).join(ranked, ranked.c.scenario_id == model.id)\
                .filter(ranked.c.position <= 20).order_by(ranked.c.position):
            historical_key = str((historical.audit_json or {}).get("world_key") or historical.slug or "")
            histories.setdefault((int(historical.subject_id), historical_key), []).append(historical)
    for row, subject in rows[:bounded_limit]:
        world_key = str((row.audit_json or {}).get("world_key") or row.slug or "")
        label = str(subject.display_name or subject.slug) if subject is not None else str(row.subject_id)
        view = projections[int(row.id)]
        scenario = serialize_scenario(row)
        if view["compacted"] or count_tokens(scenario) > POLICIES["world"].ceiling_tokens:
            scenario = {name: scenario.get(name) for name in (
                "id", "subject_id", "slug", "name", "status", "scenario_kind", "probability", "confidence", "selected_canon", "audit")}
            scenario.update(structured_fields_omitted=True, protected_field_count=len(view["protected_fields"]),
                            source_locator={"kind": "world", "object_id": int(row.id)})
        item = {"world_key": world_key, "subject": label, "scenario": scenario,
                "budget": view["budget"], "source_locator": {"kind": "world", "object_id": int(row.id)},
                "compacted": view["compacted"], "source_hash": view["source_hash"]}
        if view["compacted"] or scenario.get("structured_fields_omitted"):
            item["content_md"] = view["content_md"]
            if view.get("source_page"):
                item["source_page"] = {name: value for name, value in view["source_page"].items() if name != "text"}
        item["version_count"] = version_counts[(int(row.subject_id), world_key)]
        if include_history:
            item["history"] = []
            for previous in histories.get((int(row.subject_id), world_key), []):
                original = serialize_scenario(previous)
                if count_tokens(original) > POLICIES["world"].ceiling_tokens:
                    original = {"id": int(previous.id), "audit": previous.audit_json or {},
                                "source_locator": {"kind": "world", "object_id": int(previous.id)},
                                "structured_fields_omitted": True}
                if count_tokens(item["history"] + [original]) > POLICIES["world"].ceiling_tokens:
                    item["history_truncated"] = True
                    break
                item["history"].append(original)
            item["history_truncated"] = item.get("history_truncated", False) or item["version_count"] > len(item["history"])
        if count_tokens({"worlds": out + [item]}) > POLICIES["world_catalog"].ceiling_tokens - 200:
            omitted += 1
            continue
        out.append(item)
    return {"count": len(out), "worlds": out, "truncated": len(rows) > bounded_limit or bool(omitted),
            "omitted_due_to_budget": omitted, "retrieval_limit": bounded_limit,
            "budget": budget_status("world_catalog", {"worlds": out}), "all_stored_versions_scanned": False}


def get_worldview_reconciliation(
    session: Session, *, subject_id: int, world_key: str, limit: int = 40,
) -> dict[str, Any]:
    """Put a named world beside exact-subject estimates and model coverage.

    Metric-name coverage is deliberately weaker than numeric agreement: period,
    unit, product scope, and premises still require the agent's judgment.
    """
    canonical = _latest_world(session, subject_id=subject_id, world_key=world_key)
    if canonical is None:
        raise ValueError("World not found")
    from thoth_terminal.research_canon_service import serialize_scenario
    scenario = serialize_scenario(canonical)
    worlds = list_worldview_worlds(session, subject_id=subject_id, query=world_key, limit=100)
    world = next((item for item in worlds["worlds"] if item["world_key"] == world_key), None)
    if world is None:
        world = {"world_key": world_key, "source_locator": {"kind": "world", "object_id": int(canonical.id)},
                 "structured_fields_omitted": True}

    from thoth_terminal.web.app_sections.consensus import _consensus_payload
    from thoth_terminal.memory_dataset_resolver import list_memory_system_inputs

    bounded_limit = max(5, min(int(limit), 80))
    consensus = _consensus_payload(session, subject_id=subject_id, limit=bounded_limit)
    metric_keys = {str(key).casefold() for key in (scenario.get("key_metrics") or {})}
    estimates = []
    for estimate in consensus["consensus_rows"][:bounded_limit]:
        estimates.append({**estimate, "represented_in_world": str(estimate.get("metric") or "").casefold() in metric_keys})
    comparisons = []
    for metric_key, metric in (scenario.get("key_metrics") or {}).items():
        comparison = {"metric_key": str(metric_key), "status": "not_comparable"}
        if not isinstance(metric, dict):
            comparison["status"] = "not_applicable"
            comparison["reason"] = "Descriptive world value has no estimate dimensions"
            comparisons.append(comparison)
            continue
        if isinstance(metric.get("value"), bool):
            comparison["reason"] = "World metric needs a numeric value and explicit dimensions"
            comparisons.append(comparison)
            continue
        try:
            world_value = float(metric["value"])
        except (KeyError, TypeError, ValueError):
            comparison["reason"] = "World metric needs a numeric value"
            comparisons.append(comparison)
            continue
        if not math.isfinite(world_value):
            comparison["reason"] = "World metric value must be finite"
            comparisons.append(comparison)
            continue
        required = ("period", "unit")
        if any(not str(metric.get(key) or "").strip() for key in required):
            comparison["reason"] = "World metric needs explicit period and unit"
            comparisons.append(comparison)
            continue
        same_metric = [row for row in consensus["consensus_rows"] if str(row.get("metric") or "").casefold() == str(metric_key).casefold()]
        candidates = []
        for row in same_metric:
            if str(row.get("period") or "").casefold() != str(metric["period"]).casefold():
                continue
            if str(row.get("unit") or "").casefold() != str(metric["unit"]).casefold():
                continue
            if str(row.get("currency") or "").casefold() != str(metric.get("currency") or "").casefold():
                continue
            if str(row.get("product_category") or "").casefold() != str(metric.get("product_category") or "").casefold():
                continue
            if str(row.get("scenario") or "").casefold() != str(metric.get("source_scenario") or "").casefold():
                continue
            if str(row.get("period_basis") or "").casefold() != str(metric.get("period_basis") or "").casefold():
                continue
            if str(row.get("value_type") or "").casefold() != str(metric.get("value_type") or "").casefold():
                continue
            candidates.append(row)
        if not candidates:
            comparison["reason"] = "No estimate has the same metric, period, unit, currency, product scope, scenario, and value basis"
        elif len(candidates) != 1:
            comparison["status"] = "ambiguous"
            comparison["reason"] = "Several estimate groups match; select a lane and period basis before comparing"
            comparison["candidate_count"] = len(candidates)
        else:
            row = candidates[0]
            estimate_value = row.get("consensus_value")
            if estimate_value is None:
                comparison["reason"] = "Matched estimate has no numeric aggregate"
            else:
                difference = world_value - float(estimate_value)
                comparison.update(
                    status="comparable", world_value=world_value,
                    consensus_value=float(estimate_value), difference=difference,
                    difference_fraction=(difference / abs(float(estimate_value)) if float(estimate_value) else None),
                    period=row["period"], unit=row["unit"], currency=row["currency"],
                    product_category=row["product_category"], lane=row["lane"],
                    source_count=row["source_count"], warnings=row["warnings"],
                )
        comparisons.append(comparison)
    memory_inputs = None
    subject = session.get(ResearchSubject, subject_id)
    subject_text = " ".join((str(subject.display_name or ""), str(subject.slug or ""),
                             str(subject.subject_key or ""))).casefold() if subject is not None else ""
    premises = (scenario.get("assumptions") or {}).get("premises") or []
    model_refs = {str(ref).casefold() for premise in premises if isinstance(premise, dict)
                  for ref in (premise.get("model_refs") or [])}
    if any(term in subject_text for term in ("memory", "dram", "hbm", "nand")) or "memory_system" in model_refs:
        memory_inputs = list_memory_system_inputs(session)
        published = {row["family"] for row in memory_inputs["datasets"] if row["latest_published_version_id"] is not None}
        memory_inputs["missing_physical_input_families"] = sorted({
            "memory_site_program_ramp", "hbm_stack_conversion", "hbm_supplier_qualification_allocation",
        } - published)
    return {
        "world": world,
        "consensus_rows": estimates,
        "world_metric_comparisons": comparisons,
        "evidence_rows": consensus["evidence_rows"][:bounded_limit],
        "consensus_metrics": consensus["metrics"],
        "memory_inputs": memory_inputs,
        "truncated": consensus["metrics"]["evidence_rows"] > bounded_limit or len(consensus["consensus_rows"]) > bounded_limit,
    }


def apply_worldview_scenario(
    session: Session, *, cloud_run_id: int, capability: str, operation_key: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Apply premise and metric deltas as a new immutable version of one world."""
    if not 1 <= len(operation_key.strip()) <= 120:
        raise ValueError("operation_key must be 1-120 characters")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    if len(json.dumps(payload, ensure_ascii=False, default=str)) > 100_000:
        raise ValueError("Scenario update exceeds the 100,000-character limit")
    run = _authorized_run(session, cloud_run_id=cloud_run_id, capability=capability)
    replay = _replayed_receipt(session, cloud_run_id=cloud_run_id, event_kind="worldview_scenario_write", operation_key=operation_key)
    if replay is not None:
        return replay
    source_ids = payload.get("source_document_ids")
    if not isinstance(source_ids, list) or not source_ids or any(not isinstance(value, int) or value < 1 for value in source_ids):
        raise ValueError("source_document_ids must contain stored document IDs")
    source_ids = sorted(set(source_ids))
    found = {int(row[0]) for row in session.query(Document.id).filter(Document.id.in_(source_ids)).all()}
    if found != set(source_ids):
        raise ValueError("source_document_ids must resolve to stored documents")
    subject_id = payload.get("subject_id")
    if not isinstance(subject_id, int) or session.get(ResearchSubject, subject_id) is None:
        raise ValueError("subject_id must identify an existing research subject")
    world_key = _world_key(payload.get("world_key"))
    change_summary = str(payload.get("change_summary") or "").strip()
    if len(change_summary) < 20:
        raise ValueError("change_summary must explain the model change")
    prior = _latest_world(session, subject_id=subject_id, world_key=world_key)
    fork_id = payload.get("fork_scenario_id")
    if prior is not None and fork_id is not None:
        raise ValueError("fork_scenario_id is only valid for a new world")
    fork = session.get(ResearchSubjectScenario, int(fork_id)) if isinstance(fork_id, int) else None
    if fork_id is not None and (fork is None or int(fork.subject_id) != subject_id):
        raise ValueError("fork_scenario_id must identify a scenario for this subject")
    base = prior or fork
    base_assumptions = dict(base.assumptions_json or {}) if base is not None else {}
    premises = {str(item.get("key")): dict(item) for item in base_assumptions.get("premises", [])
                if isinstance(item, dict) and str(item.get("key") or "")}
    changes = payload.get("premise_changes") or []
    if not isinstance(changes, list) or len(changes) > 40:
        raise ValueError("premise_changes must be a list of at most 40 items")
    changed_keys: list[str] = []
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("Each premise change must be an object")
        key = _premise_key(change.get("key"))
        action = str(change.get("action") or "upsert").strip().lower()
        if action == "remove":
            if key not in premises:
                raise ValueError(f"Cannot remove missing premise {key}")
            premises.pop(key)
        elif action == "upsert":
            statement = str(change.get("statement") or "").strip()
            if not 12 <= len(statement) <= 4000:
                raise ValueError("Premise statement must contain 12-4000 characters")
            cited = change.get("source_document_ids") or source_ids
            if not isinstance(cited, list) or any(value not in source_ids for value in cited):
                raise ValueError("Changed premise citations must come from source_document_ids")
            premises[key] = {**premises.get(key, {}), **{k: v for k, v in change.items() if k != "action"},
                             "key": key, "statement": statement, "source_document_ids": sorted(set(cited))}
        else:
            raise ValueError("Premise action must be upsert or remove")
        changed_keys.append(key)
    if not premises and not prior and not payload.get("metric_changes"):
        raise ValueError("A new world needs a premise or metric change")
    if not changes and not payload.get("metric_changes") and not payload.get("thesis_summary"):
        raise ValueError("A world revision needs a premise, metric, or thesis change")
    assumptions = {**base_assumptions, "premises": list(premises.values())}
    metrics = dict(base.key_metrics_json or {}) if base is not None else {}
    metric_changes = payload.get("metric_changes") or {}
    if not isinstance(metric_changes, dict) or len(metric_changes) > 80:
        raise ValueError("metric_changes must be an object of at most 80 items")
    for key, value in metric_changes.items():
        if value is None:
            metrics.pop(str(key), None)
        else:
            metrics[str(key)] = {
                **(value if isinstance(value, dict) else {"value": value}),
                "source_document_ids": source_ids,
            }
    summary = str(payload.get("thesis_summary") or (base.thesis_summary if base is not None else "") or "").strip()
    if len(summary) < 20:
        raise ValueError("thesis_summary must explain this coherent world")
    confidence = payload.get("confidence", prior.confidence if prior is not None else None)
    if confidence is None or not math.isfinite(float(confidence)) or not 0 <= float(confidence) <= 1:
        raise ValueError("confidence must be between 0 and 1")
    probability = payload.get("probability", prior.probability if prior is not None else None)
    if probability is not None and (not math.isfinite(float(probability)) or not 0 <= float(probability) <= 1):
        raise ValueError("probability must be between 0 and 1")
    selected = bool(payload.get("selected_canon", prior.selected_canon if prior is not None else False))
    if prior is not None and prior.selected_canon and not selected:
        raise ValueError("A selected world revision must remain selected; select another world separately")
    if selected and len(str(payload.get("selection_rationale") or "").strip()) < 30:
        raise ValueError("Selecting a base world requires a reasoned selection_rationale")
    version = int((prior.audit_json or {}).get("world_version") or 0) + 1 if prior is not None else 1
    slug = f"{world_key[:110]}-v{version:04d}"
    if session.query(ResearchSubjectScenario.id).filter_by(subject_id=subject_id, slug=slug).first():
        raise ValueError("World version slug already exists")
    from thoth_terminal.research_canon_service import upsert_subject_scenario

    links = list(base.evidence_links_json or []) if base is not None else []
    links.extend({"source_document_id": doc_id, "relation": "supports_revision", "change_summary": change_summary}
                 for doc_id in source_ids)
    row = upsert_subject_scenario(
        session, subject_id=subject_id, slug=slug,
        name=str(payload.get("name") or (base.name if base is not None else world_key)).strip(),
        scenario_kind=str(payload.get("scenario_kind") or (prior.scenario_kind if prior is not None else "custom")),
        probability=float(probability) if probability is not None else None,
        confidence=float(confidence), selected_canon=selected, thesis_summary=summary,
        assumptions=assumptions, key_metrics=metrics, evidence_links=links,
        invalidation_triggers=payload.get("invalidation_triggers", list(base.invalidation_triggers_json or []) if base is not None else []),
        open_questions=payload.get("open_questions", list(base.open_questions_json or []) if base is not None else []),
        status="active", audit={"world_key": world_key, "world_version": version,
                                "revision_source_document_ids": source_ids, "revision_summary": change_summary,
                                "selection_rationale": str(payload.get("selection_rationale") or "").strip() or None,
                                "cloud_goal_run_id": cloud_run_id, "operation_key": operation_key},
    )
    row.supersedes_scenario_id = int(base.id) if base is not None else None
    if prior is not None:
        prior.status = "superseded"
        prior.selected_canon = False
    session.flush()
    receipt = {"cloud_run_id": cloud_run_id, "operation_key": operation_key,
               "subject_id": subject_id, "world_key": world_key, "world_version": version,
               "scenario_id": int(row.id), "previous_scenario_id": int(base.id) if base is not None else None,
               "selected_canon": bool(row.selected_canon), "changed_premises": changed_keys,
               "changed_metrics": list(metric_changes), "source_document_ids": source_ids, "replayed": False}
    from thoth_terminal.research_state_compaction_service import render_compacted_state
    projection = render_compacted_state(session, "world", int(row.id))
    receipt.update(budget=projection["budget"], source_hash=projection["source_hash"],
                   source_locator={"kind": "world", "object_id": int(row.id)}, compacted=projection["compacted"])
    append_cloud_goal_event(session, run, event_kind="worldview_scenario_write",
                            summary=change_summary, payload={"operation_key": operation_key, "receipt": receipt})
    return receipt


def apply_worldview_roadmap_update(
    session: Session,
    *,
    cloud_run_id: int,
    capability: str,
    operation_key: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Record one sourced future claim or a new mention of an existing claim."""
    if not 1 <= len(operation_key.strip()) <= 120:
        raise ValueError("operation_key must be 1-120 characters")
    if not isinstance(payload, dict) or "updates" in payload or "entries" in payload:
        raise ValueError("A worldview roadmap write must contain exactly one entry")
    if "mention_key" in payload or "roadmap_mention_key" in payload:
        raise ValueError("The roadmap service owns mention keys")
    source_document_id = payload.get("source_document_id")
    if not isinstance(source_document_id, int) or source_document_id < 1:
        raise ValueError("source_document_id must identify a stored source document")
    if session.query(Document.id).filter_by(id=source_document_id).first() is None:
        raise ValueError("source_document_id must identify a stored source document")
    title = str(payload.get("title") or "").strip()
    statement = str(payload.get("statement") or "").strip()
    if not 3 <= len(title) <= 255 or len(statement) < 20:
        raise ValueError("A roadmap update needs a specific title and statement")
    if not any(payload.get(key) for key in ("expected_window", "target_date", "target_year")):
        raise ValueError("A roadmap update needs an expected window or target date/year")
    confidence = payload.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("confidence must be a number between 0 and 1")
    if str(payload.get("model_use") or "") not in {"watch_only", "scenario_input", "base_input"}:
        raise ValueError("model_use must be watch_only, scenario_input, or base_input")
    if confidence < 0.7 and payload["model_use"] == "base_input":
        raise ValueError("An uncertain roadmap claim cannot be a base_input")
    if confidence < 0.7 and not str(payload.get("uncertainty") or "").strip():
        raise ValueError("An uncertain roadmap update needs an explicit uncertainty")
    run = _authorized_run(session, cloud_run_id=cloud_run_id, capability=capability)
    replay = _replayed_receipt(session, cloud_run_id=cloud_run_id, event_kind="worldview_roadmap_update", operation_key=operation_key)
    if replay is not None:
        return replay
    metadata = dict(payload.get("payload") or {}) if isinstance(payload.get("payload"), dict) else {}
    metadata.update(cloud_goal_run_id=int(cloud_run_id), worldview_operation_key=operation_key)
    update = {**payload, "payload": metadata}
    result = upsert_roadmap_update(session, payload=update, actor="worldview_agent", cloud_run_id=int(cloud_run_id))
    applied = result["applied"]
    if len(applied) != 1:
        raise ValueError("Roadmap write did not produce exactly one entry")
    receipt = {
        "cloud_run_id": int(cloud_run_id), "operation_key": operation_key,
        **applied[0], "replayed": False,
    }
    append_cloud_goal_event(
        session, run, event_kind="worldview_roadmap_update",
        summary=title, payload={"operation_key": operation_key, "receipt": receipt},
    )
    return receipt


def apply_worldview_roadmap_review(
    session: Session, *, cloud_run_id: int, capability: str,
    operation_key: str, payload: dict[str, Any],
) -> dict[str, Any]:
    """Review one immutable forecast with the scheduled run's write capability."""
    from thoth_terminal.roadmap_outcome_service import review_roadmap_forecast

    if not 1 <= len(operation_key.strip()) <= 120:
        raise ValueError("operation_key must be 1-120 characters")
    if not isinstance(payload, dict):
        raise ValueError("payload must be one forecast review")
    allowed = {"entry_key", "forecast_mention_id", "outcome", "rationale", "source_document_ids",
               "confidence", "observed_window", "actual_date", "uncertainty"}
    if set(payload) - allowed:
        raise ValueError("Unknown roadmap review fields")
    if not str(payload.get("entry_key") or "").strip() or type(payload.get("forecast_mention_id")) is not int:
        raise ValueError("A review needs an entry_key and forecast_mention_id")
    run = _authorized_run(session, cloud_run_id=cloud_run_id, capability=capability)
    # The service verifies the complete request on retries, including changed
    # outcomes or evidence. An event lookup alone would conceal a reused key.
    result = review_roadmap_forecast(
        session, cloud_run_id=int(cloud_run_id), operation_key=operation_key,
        entry_key=str(payload["entry_key"]), forecast_mention_id=payload["forecast_mention_id"],
        outcome=payload.get("outcome"), rationale=payload.get("rationale"),
        source_document_ids=payload.get("source_document_ids", []), confidence=payload.get("confidence"),
        observed_window=payload.get("observed_window"), actual_date=payload.get("actual_date"),
        uncertainty=payload.get("uncertainty"),
    )
    receipt = {**result, "cloud_run_id": int(cloud_run_id), "operation_key": operation_key,
               "replayed": not result["created"]}
    if result["created"]:
        append_cloud_goal_event(
            session, run, event_kind="worldview_roadmap_review",
            summary=f"Roadmap outcome: {result['outcome']}",
            payload={"operation_key": operation_key, "receipt": receipt},
        )
    return receipt


def send_worldview_operator_message(
    session: Session, *, cloud_run_id: int, capability: str,
    operation_key: str, payload: dict[str, Any],
) -> dict[str, Any]:
    """Publish private research posts, requests and replies to the owner inbox."""
    from thoth_terminal.operator_research_relay_service import record_worldview_operator_message

    if not isinstance(operation_key, str) or not 1 <= len(operation_key.strip()) <= 120:
        raise ValueError("operation_key must be 1-120 characters")
    allowed = {"kind", "text", "rationale", "source_document_ids", "reply_to_request_id",
               "disposition", "write_operation_keys", "title", "assessment"}
    if not isinstance(payload, dict) or set(payload) - allowed:
        raise ValueError("payload must contain one operator message with supported fields")
    run = _authorized_run(session, cloud_run_id=cloud_run_id, capability=capability)
    receipt = record_worldview_operator_message(
        session, cloud_run_id=int(cloud_run_id), operation_key=operation_key,
        kind=payload.get("kind"), text=payload.get("text"), rationale=payload.get("rationale"),
        source_document_ids=payload.get("source_document_ids", []),
        reply_to_request_id=payload.get("reply_to_request_id"),
        disposition=payload.get("disposition"), write_operation_keys=payload.get("write_operation_keys", []),
        title=payload.get("title"), assessment=payload.get("assessment"),
    )
    if not receipt["replayed"]:
        append_cloud_goal_event(
            session, run, event_kind="worldview_operator_message",
            summary=receipt.get("title") or receipt["kind"].replace("_", " ").title(),
            payload={"operation_key": operation_key, "receipt": receipt},
        )
    return receipt


def resolve_worldview_research_request(
    session: Session, *, cloud_run_id: int, capability: str,
    request_id: int, write_operation_key: str, note: str,
) -> dict[str, Any]:
    """Acknowledge a queued source only after this run wrote from that source."""
    _authorized_run(session, cloud_run_id=cloud_run_id, capability=capability)
    return resolve_evidence_request(
        session, request_id=request_id, decision="reviewed", operator_note=note,
        operation_key=worldview_resolution_key(cloud_run_id, write_operation_key),
    )


def apply_worldview_journal(
    session: Session,
    *,
    cloud_run_id: int,
    capability: str,
    operation_key: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Append a sourced, uncertainty-aware long-form research note."""
    if not 1 <= len(operation_key.strip()) <= 120:
        raise ValueError("operation_key must be 1-120 characters")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    title = str(payload.get("title") or "").strip()
    content = str(payload.get("content_md") or "").strip()
    source_ids = payload.get("source_document_ids")
    if not title or len(content) < 100:
        raise ValueError("A journal entry needs a title and at least 100 characters of analysis")
    if not isinstance(source_ids, list) or not source_ids or any(not isinstance(value, int) or value < 1 for value in source_ids):
        raise ValueError("A journal entry needs source_document_ids")
    found_ids = {int(row[0]) for row in session.query(Document.id).filter(Document.id.in_(source_ids)).all()}
    if found_ids != set(source_ids):
        raise ValueError("Journal source_document_ids must resolve to stored documents")
    run = _authorized_run(session, cloud_run_id=cloud_run_id, capability=capability)
    replay = _replayed_receipt(session, cloud_run_id=cloud_run_id, event_kind="worldview_journal_write", operation_key=operation_key)
    if replay is not None:
        return replay
    metadata = dict(payload.get("payload") or {}) if isinstance(payload.get("payload"), dict) else {}
    metadata.update(source_document_ids=sorted(set(source_ids)), cloud_goal_run_id=int(cloud_run_id))
    result = upsert_informal_research_doc(
        session,
        payload={**payload, "category": "worldview_journal", "payload": metadata},
        actor="worldview_agent", cloud_run_id=int(cloud_run_id), event_kind="worldview_journal",
    )
    row = result["document"]
    event = result["event"]
    row.payload = {
        **dict(row.payload or {}),
        "source_document_ids": sorted(set((row.payload or {}).get("source_document_ids", [])) | set(source_ids)),
        "cloud_goal_run_id": int(cloud_run_id),
    }
    event.payload = {**dict(event.payload or {}), "source_document_ids": sorted(set(source_ids)), "operation_key": operation_key}
    receipt = {
        "cloud_run_id": int(cloud_run_id), "operation_key": operation_key,
        "informal_doc_id": int(row.id), "informal_doc_key": row.doc_key,
        "title": row.title, "subject": row.subject, "category": row.category,
        "event_id": int(event.id), "words_added": int(result["words_added"]),
        "word_count": int(row.word_count), "replayed": False,
    }
    append_cloud_goal_event(
        session, run, event_kind="worldview_journal_write",
        summary=title[:255], payload={"operation_key": operation_key, "receipt": receipt},
    )
    return receipt


def apply_worldview_consensus_evidence(
    session: Session,
    *,
    cloud_run_id: int,
    capability: str,
    operation_key: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Add one sourced forecast to the existing analyst-evidence consensus spine.

    Forecast revisions are separate observations. The consensus view chooses
    the latest eligible observation per estimate owner and preserves the older
    rows as history. A deterministic content key also deduplicates rediscovery
    of the same claim across scheduled runs.
    """
    if not 1 <= len(operation_key.strip()) <= 120:
        raise ValueError("operation_key must be 1-120 characters")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    run = _authorized_run(session, cloud_run_id=cloud_run_id, capability=capability)
    replay = _replayed_receipt(session, cloud_run_id=cloud_run_id, event_kind="worldview_consensus_evidence", operation_key=operation_key)
    if replay is not None:
        return replay
    row, created, evidence_key, normalized, serialized = upsert_sourced_consensus_evidence(
        session, payload=payload,
        metadata={"worldview_cloud_run_id": int(cloud_run_id), "worldview_operation_key": operation_key},
    )
    receipt = {
        "cloud_run_id": int(cloud_run_id), "operation_key": operation_key,
        "analyst_evidence_id": int(row.id), "evidence_key": evidence_key,
        "created": created, "subject_id": serialized["subject_id"],
        "estimate_owner": normalized["estimate_owner"],
        "estimate_metric": serialized["estimate_metric"],
        "estimate_period": serialized["estimate_period"],
        "estimate_value": serialized["estimate_value"],
        "replayed": False,
    }
    append_cloud_goal_event(
        session, run, event_kind="worldview_consensus_evidence",
        summary=str(normalized["summary"])[:255], payload={"operation_key": operation_key, "receipt": receipt},
    )
    return receipt


def _consensus_subject_from_name(session: Session, *, name: str, kind: str) -> ResearchSubject:
    slug_base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:100].strip("-")
    if not slug_base:
        raise ValueError("subject_name must contain letters or digits")
    key = f"worldview:{kind}:{slug_base}"
    subject = session.query(ResearchSubject).filter_by(subject_key=key).first()
    if subject is None:
        matches = session.query(ResearchSubject).filter(ResearchSubject.kind == kind, ResearchSubject.display_name.ilike(name)).limit(2).all()
        if len(matches) > 1:
            raise ValueError("subject_name matches multiple subjects; supply subject_id")
        subject = matches[0] if matches else None
    if subject is None:
        slug = slug_base
        suffix = 2
        while session.query(ResearchSubject.id).filter_by(slug=slug).first() is not None:
            slug = f"{slug_base[:94]}-{suffix}"
            suffix += 1
        now = utcnow()
        subject = ResearchSubject(
            slug=slug, subject_key=key, kind=kind, display_name=name,
            status="active", extra={"created_from": "consensus_evidence"},
            created_at=now, updated_at=now,
        )
        session.add(subject)
        session.flush()
    if subject.status != "active":
        raise ValueError("Consensus subject is not active")
    return subject


def upsert_sourced_consensus_evidence(
    session: Session, *, payload: dict[str, Any], metadata: dict[str, Any],
) -> tuple[AnalystEvidence, bool, str, dict[str, Any], dict[str, Any]]:
    """Shared source and revision contract for scheduled and interactive forecasts."""
    source_document_id = payload.get("source_document_id")
    if not isinstance(source_document_id, int) or source_document_id < 1:
        raise ValueError("source_document_id must identify a stored source document")
    source_document = session.query(Document).filter_by(id=source_document_id).first()
    if source_document is None:
        raise ValueError("source_document_id must identify a stored source document")
    normalized = dict(payload)
    has_subject_name = bool(payload.get("subject_name"))
    if has_subject_name:
        if any(payload.get(key) for key in ("subject_id", "company_id", "issuer_id", "ticker", "company_name", "issuer_name")):
            raise ValueError("subject_name cannot be combined with another subject selector")
        name = " ".join(str(payload["subject_name"]).split())
        kind = str(payload.get("subject_kind") or "").strip().lower()
        if kind not in {"market", "technology", "product", "sector", "theme"} or not 3 <= len(name) <= 160:
            raise ValueError("subject_name needs a bounded market, technology, product, sector, or theme kind")
    if not has_subject_name and not any(normalized.get(key) for key in ("subject_id", "company_id", "issuer_id", "ticker", "company_name", "issuer_name")):
        raise ValueError("A forecast needs a resolvable research subject, company, or issuer")
    for key in ("estimate_owner", "estimate_owner_type", "estimate_metric", "estimate_period", "estimate_unit", "summary"):
        if not str(payload.get(key) or "").strip():
            raise ValueError(f"{key} is required for consensus evidence")
    if str(payload["estimate_owner_type"]).strip().lower() not in {
        "sell_side", "buy_side", "issuer_guidance", "specialist_research", "media",
    }:
        raise ValueError("estimate_owner_type must identify a consensus lane")
    low_raw, high_raw = payload.get("estimate_low"), payload.get("estimate_high")
    has_low, has_high = low_raw is not None, high_raw is not None
    if has_low != has_high:
        raise ValueError("estimate_low and estimate_high must be supplied together")
    low = high = None
    if has_low:
        try:
            low, high = float(low_raw), float(high_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("estimate range bounds must be numeric") from exc
        if not math.isfinite(low) or not math.isfinite(high) or low > high:
            raise ValueError("estimate range bounds must be finite and ordered")
    point_raw = payload.get("estimate_value")
    if point_raw is None and low is None:
        raise ValueError("estimate_value or both estimate range bounds are required")
    try:
        value = float(point_raw) if point_raw is not None else (low + high) / 2
    except (TypeError, ValueError) as exc:
        raise ValueError("estimate_value must be numeric") from exc
    if not math.isfinite(value) or (low is not None and not low <= value <= high):
        raise ValueError("estimate_value must be finite and within its supplied range")
    if payload.get("evidence_key"):
        raise ValueError("evidence_key is assigned by the sourced consensus tool to preserve revisions")
    published_at = payload.get("published_at") or (source_document.published_at.isoformat() if source_document.published_at else None)
    if not published_at:
        raise ValueError("A forecast needs a published_at date for revision ordering")
    try:
        published_at = datetime.fromisoformat(str(published_at).replace("Z", "+00:00")).isoformat()
    except ValueError as exc:
        raise ValueError("published_at must be an ISO date or datetime") from exc
    if has_subject_name:
        normalized["subject_id"] = int(_consensus_subject_from_name(session, name=name, kind=kind).id)
    forecast = dict(payload.get("forecast") or {}) if isinstance(payload.get("forecast"), dict) else {}
    if low is not None:
        forecast.update(range_low=low, range_high=high,
                        value_basis="reported_point" if point_raw is not None else "range_midpoint")
    normalized.update(published_at=published_at, estimate_value=value, forecast=forecast)
    normalized.pop("allow_remote_symbol_lookup", None)
    identity = {
        "source_document_id": source_document_id,
        "subject": [normalized.get(key) for key in ("subject_id", "company_id", "issuer_id", "ticker", "company_name", "issuer_name")],
        "estimate_owner": str(normalized["estimate_owner"]).strip().casefold(),
        "estimate_metric": str(normalized["estimate_metric"]).strip().casefold(),
        "estimate_period": str(normalized["estimate_period"]).strip().casefold(),
        "estimate_unit": str(normalized["estimate_unit"]).strip().casefold(),
        "estimate_value": value,
        "estimate_low": low,
        "estimate_high": high,
        "published_at": published_at,
        "scenario": normalized.get("scenario"),
        "product_family": normalized.get("product_family"),
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()[:32]
    evidence_key = f"worldview:{digest}"
    existing = session.query(AnalystEvidence).filter_by(evidence_key=evidence_key).first()
    if existing is not None:
        row = existing
        created = False
    else:
        source_metadata = dict(normalized.get("payload") or {}) if isinstance(normalized.get("payload"), dict) else {}
        source_metadata.update(metadata)
        normalized.update(evidence_key=evidence_key, payload=source_metadata)
        # The canonical service resolves source identity, validates modelability,
        # writes provenance edges and refreshes institutional/dataset projections.
        row = upsert_analyst_evidence(session, normalized, actor="local_agent")
        created = True
    serialized = serialize_analyst_evidence(row)
    return row, created, evidence_key, normalized, serialized
