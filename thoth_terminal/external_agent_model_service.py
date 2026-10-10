"""Bounded, explicitly released model projections for outside agents."""

from __future__ import annotations

import json
import math
import re
from datetime import date
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.models import (
    AnalystEvidence, BottleneckModel, Dataset, Document, ExternalAgentEvidence, ExternalAgentPrincipal,
    ExternalAgentProposal, ExternalModelProjection, ExternalModelWithdrawal,
    InteractiveAgentOperation, LocalAgentWriteDiff,
    ResearchReport, ResearchSubject, SupplyChain,
)


MODEL_TYPES = {
    "dataset": Dataset,
    "supply_chain": SupplyChain,
    "bottleneck": BottleneckModel,
    "research_report": ResearchReport,
    "consensus": ResearchSubject,
}
_MODEL_KEY = re.compile(r"^[a-z0-9][a-z0-9:_-]{2,254}$")


def _text(value: object, *, name: str, maximum: int, required: bool = True) -> str:
    result = str(value or "").strip()
    if (required and not result) or len(result) > maximum:
        raise ValueError(f"{name} must be {'1-' if required else '0-'}{maximum} characters")
    return result


def _projection_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) - {"title", "summary", "as_of", "metrics", "caveats", "source_labels"}:
        raise ValueError("Projection has unsupported fields")
    title = _text(payload.get("title"), name="title", maximum=255)
    summary = _text(payload.get("summary"), name="summary", maximum=1200)
    as_of = _text(payload.get("as_of"), name="as_of", maximum=10)
    try:
        date.fromisoformat(as_of)
    except ValueError as exc:
        raise ValueError("as_of must be an ISO date") from exc
    raw_metrics = payload.get("metrics")
    if not isinstance(raw_metrics, list) or not 1 <= len(raw_metrics) <= 40:
        raise ValueError("Projection requires 1-40 structured metrics")
    metrics: list[dict[str, Any]] = []
    for item in raw_metrics:
        if not isinstance(item, dict) or set(item) - {"name", "value", "unit", "period", "scenario", "low", "high", "confidence"}:
            raise ValueError("Metric has unsupported fields")
        metric = {
            "name": _text(item.get("name"), name="metric name", maximum=128),
            "unit": _text(item.get("unit"), name="metric unit", maximum=64),
            "period": _text(item.get("period"), name="metric period", maximum=64),
            "scenario": _text(item.get("scenario"), name="scenario", maximum=64, required=False),
        }
        for field in ("value", "low", "high", "confidence"):
            value = item.get(field)
            if value is None and field != "value":
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValueError(f"metric {field} must be finite and numeric")
            metric[field] = float(value)
        if "low" in metric and "high" in metric and metric["low"] > metric["high"]:
            raise ValueError("metric low cannot exceed high")
        if "confidence" in metric and not 0 <= metric["confidence"] <= 1:
            raise ValueError("metric confidence must be between 0 and 1")
        metrics.append(metric)
    caveats = payload.get("caveats") or []
    labels = payload.get("source_labels") or []
    if not isinstance(caveats, list) or len(caveats) > 10 or not isinstance(labels, list) or not 1 <= len(labels) <= 20:
        raise ValueError("Projection requires 1-20 source labels and at most 10 caveats")
    return {
        "title": title, "summary": summary, "as_of": as_of, "metrics": metrics,
        "caveats": [_text(item, name="caveat", maximum=300) for item in caveats],
        "source_labels": [_text(item, name="source label", maximum=120) for item in labels],
    }


def publish_model_projection(
    session: Session, *, model_key: str, model_type: str, model_id: int,
    payload: dict[str, Any], source_document_ids: list[int],
) -> ExternalModelProjection:
    key = str(model_key or "").strip().lower()
    if not _MODEL_KEY.fullmatch(key):
        raise ValueError("model_key must be a stable lowercase model identifier")
    model_cls = MODEL_TYPES.get(str(model_type or ""))
    if model_cls is None or not isinstance(model_id, int) or model_id < 1:
        raise ValueError("Unsupported model type or ID")
    model = session.query(model_cls).filter(model_cls.id == model_id).first()
    if model is None:
        raise ValueError("Referenced Thoth model does not exist")
    if model_type == "dataset":
        classification = " ".join(str(value or "").lower() for value in
                                  (model.slug, model.category, model.family,
                                   (model.extra or {}).get("data_classification")))
        if re.search(r"\b(portfolio|holdings|positions|pnl|p&l|trade-ledger|personal-finance)\b", classification):
            raise ValueError("Personal portfolio datasets cannot be released on the shared research API")
    if not isinstance(source_document_ids, list) or not source_document_ids or len(source_document_ids) > 50:
        raise ValueError("Projection requires 1-50 source document IDs")
    if any(not isinstance(value, int) or value < 1 for value in source_document_ids):
        raise ValueError("source_document_ids must be positive integers")
    source_ids = sorted(set(source_document_ids))
    documents = session.query(Document).filter(Document.id.in_(source_ids)).all()
    if len(documents) != len(source_ids):
        raise ValueError("Every projection source must be stored in Thoth")
    if any(str((doc.extra or {}).get("access_scope") or "") == "external_agent" for doc in documents):
        raise ValueError("Another user's private upload cannot support a shared model release")
    clean = _projection_payload(payload)
    # Paid/private sources may support a derived claim. Reject long copied
    # passages; only labels, derived values, and interpretation leave Thoth.
    phrases = re.split(r"[.!?;\n]+", " ".join(
        [clean["summary"], *clean["caveats"], *clean["source_labels"]]))
    for document in documents:
        original = re.sub(r"\s+", " ", str(document.normalized_text or "")).casefold()
        for phrase in phrases:
            candidate = re.sub(r"\s+", " ", phrase).strip().casefold()
            if len(candidate) >= 80 and candidate in original:
                raise ValueError("Projection contains a long verbatim source passage")
    latest = (
        session.query(ExternalModelProjection)
        .filter_by(model_key=key)
        .order_by(ExternalModelProjection.version.desc())
        .first()
    )
    if latest is not None and latest.model_type != model_type:
        raise ValueError("model_key already belongs to a different model type")
    if latest is not None and latest.model_id != model_id:
        raise ValueError("model_key already belongs to a different model ID")
    if latest is not None and latest.status == "published" and latest.payload_json == clean and latest.source_document_ids_json == source_ids:
        return latest
    row = ExternalModelProjection(
        model_key=key, version=1 if latest is None else latest.version + 1,
        model_type=model_type, model_id=model_id, payload_json=clean,
        source_document_ids_json=source_ids, status="published", created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def withdraw_model_projection(session: Session, *, model_key: str, reason: str) -> dict[str, Any]:
    key = str(model_key or "").strip().lower()
    note = _text(reason, name="reason", maximum=500)
    rows = session.query(ExternalModelProjection).filter_by(model_key=key, status="published").with_for_update().all()
    if not rows:
        raise ValueError("No published model projection found")
    now = utcnow()
    for row in rows:
        row.status = "withdrawn"
    session.add(ExternalModelWithdrawal(model_key=key, reason=note,
                                        withdrawn_versions_json=[row.version for row in rows], created_at=now))
    session.flush()
    return {"model_key": key, "withdrawn_versions": len(rows), "reason": note}


def serialize_public_projection(row: ExternalModelProjection) -> dict[str, Any]:
    return {
        "model_key": row.model_key, "version": row.version,
        "model_type": row.model_type, "model_id": row.model_id,
        "released_at": row.created_at.isoformat(), **dict(row.payload_json or {}),
    }


def list_public_projections(session: Session, *, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 50))
    latest = (
        session.query(ExternalModelProjection.model_key.label("key"),
                      func.max(ExternalModelProjection.version).label("version"))
        .filter(ExternalModelProjection.status == "published")
        .group_by(ExternalModelProjection.model_key)
        .subquery()
    )
    rows = (
        session.query(ExternalModelProjection)
        .join(latest, (ExternalModelProjection.model_key == latest.c.key) &
              (ExternalModelProjection.version == latest.c.version))
        .order_by(ExternalModelProjection.created_at.desc())
        .all()
    )
    needle = str(query or "").strip().casefold()[:100]
    selected = [row for row in rows if not needle or needle in row.model_key.casefold() or needle in str((row.payload_json or {}).get("title") or "").casefold()]
    return [serialize_public_projection(row) for row in selected[:limit]]


def get_public_projection(session: Session, *, model_key: str) -> dict[str, Any] | None:
    row = (session.query(ExternalModelProjection)
           .filter_by(model_key=str(model_key or "").strip().lower(), status="published")
           .order_by(ExternalModelProjection.version.desc()).first())
    return serialize_public_projection(row) if row is not None else None


def submit_external_model_proposal(
    session: Session, *, principal_id: int, request_key: str, target_model_key: str,
    document_ids: list[int], payload: dict[str, Any],
) -> ExternalAgentProposal:
    key = _text(request_key, name="request_key", maximum=120)
    model_key = _text(target_model_key, name="target_model_key", maximum=255).lower()
    if not _MODEL_KEY.fullmatch(model_key):
        raise ValueError("target_model_key must name a released model")
    if get_public_projection(session, model_key=model_key) is None:
        raise ValueError("Target model is not available to outside agents")
    if not isinstance(document_ids, list) or len(document_ids) > 20 or any(not isinstance(value, int) or value < 1 for value in document_ids):
        raise ValueError("document_ids must contain at most 20 positive IDs")
    owned = {int(row[0]) for row in session.query(ExternalAgentEvidence.document_id).filter(
        ExternalAgentEvidence.principal_id == int(principal_id),
        ExternalAgentEvidence.document_id.in_(document_ids),
    ).all()}
    if set(document_ids) != owned:
        raise ValueError("Proposal documents must belong to this principal")
    if not isinstance(payload, dict) or set(payload) - {"claim", "reason", "suggested_metrics", "uncertainty"}:
        raise ValueError("Proposal has unsupported fields")
    claim = _text(payload.get("claim"), name="claim", maximum=1200)
    reason = _text(payload.get("reason"), name="reason", maximum=4000)
    uncertainty = _text(payload.get("uncertainty"), name="uncertainty", maximum=1000)
    suggested = payload.get("suggested_metrics") or []
    if not isinstance(suggested, list) or len(suggested) > 20:
        raise ValueError("suggested_metrics must be a list of at most 20 items")
    if len(json.dumps(suggested, default=str)) > 10_000:
        raise ValueError("suggested_metrics is too large")
    existing = session.query(ExternalAgentProposal).filter_by(principal_id=int(principal_id), request_key=key).first()
    clean = {"claim": claim, "reason": reason, "uncertainty": uncertainty,
             "suggested_metrics": suggested}
    if existing is not None:
        if existing.target_model_key != model_key or existing.document_ids_json != sorted(set(document_ids)) or existing.payload_json != clean:
            raise ValueError("request_key was already used for a different proposal")
        return existing
    row = ExternalAgentProposal(
        principal_id=int(principal_id), request_key=key, target_model_key=model_key,
        document_ids_json=sorted(set(document_ids)), payload_json=clean,
        status="submitted", created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def serialize_operator_proposal(session: Session, row: ExternalAgentProposal) -> dict[str, Any]:
    principal = session.query(ExternalAgentPrincipal).filter_by(id=row.principal_id).one()
    docs = session.query(Document.id, Document.title).filter(Document.id.in_(row.document_ids_json or [])).all()
    return {
        "proposal_id": int(row.id), "principal": principal.slug,
        "target_model_key": row.target_model_key, "request_key": row.request_key,
        "status": row.status, "created_at": row.created_at.isoformat(),
        "payload": dict(row.payload_json or {}),
        "evidence": [{"document_id": int(doc_id), "title": title} for doc_id, title in docs],
        "operator_note": row.operator_note,
        "resolution_operation_key": row.resolution_operation_key,
    }


def list_operator_proposals(session: Session, *, status: str = "submitted", limit: int = 20) -> list[dict[str, Any]]:
    if status not in {"submitted", "reviewed", "declined", "all"}:
        raise ValueError("Invalid proposal status")
    query = session.query(ExternalAgentProposal)
    if status != "all":
        query = query.filter(ExternalAgentProposal.status == status)
    rows = query.order_by(ExternalAgentProposal.created_at.asc()).limit(max(1, min(int(limit), 50))).all()
    return [serialize_operator_proposal(session, row) for row in rows]


def resolve_operator_proposal(
    session: Session, *, proposal_id: int, decision: str,
    operator_note: str, operation_key: str | None = None,
) -> dict[str, Any]:
    if decision not in {"reviewed", "declined"}:
        raise ValueError("decision must be reviewed or declined")
    note = _text(operator_note, name="operator_note", maximum=1200)
    row = session.query(ExternalAgentProposal).filter_by(id=int(proposal_id)).with_for_update().first()
    if row is None:
        raise ValueError("Proposal not found")
    if row.status != "submitted":
        if row.status == decision and row.operator_note == note and row.resolution_operation_key == operation_key:
            return serialize_operator_proposal(session, row)
        raise ValueError("Proposal is already resolved")
    if decision == "reviewed":
        key = _text(operation_key, name="operation_key", maximum=120)
        operation = session.query(InteractiveAgentOperation).filter_by(operation_key=key).first()
        if operation is None or not operation.receipt:
            raise ValueError("A completed canonical write operation is required")
        release = session.query(ExternalModelProjection).filter_by(model_key=row.target_model_key, status="published").order_by(ExternalModelProjection.version.desc()).first()
        if release is None:
            raise ValueError("The target model is no longer released")
        receipt = dict(operation.receipt)
        if release.model_type == "consensus":
            evidence = session.query(AnalystEvidence).filter_by(id=receipt.get("analyst_evidence_id")).first()
            if evidence is None or evidence.subject_id != release.model_id:
                raise ValueError("Canonical write does not target the proposal model")
            cited_ids = {int(evidence.source_document_id)} if evidence.source_document_id else set()
        else:
            diff = session.query(LocalAgentWriteDiff).filter_by(id=receipt.get("write_diff_id")).first()
            expected_type = {"dataset": "dataset_or_data_series", "supply_chain": "supply_chain",
                             "research_report": "research_report"}.get(release.model_type)
            if diff is None or expected_type is None or diff.destination_type != expected_type or diff.destination_id != release.model_id:
                raise ValueError("Canonical write does not target the proposal model")
            cited_ids = set(diff.source_document_ids_json or [])
        if row.document_ids_json and not (set(row.document_ids_json) & cited_ids):
            raise ValueError("Canonical write must cite the proposal's evidence")
        row.resolution_operation_key = key
    elif operation_key:
        raise ValueError("A declined proposal cannot cite a write operation")
    row.status = decision
    row.operator_note = note
    row.resolved_at = utcnow()
    session.flush()
    return serialize_operator_proposal(session, row)
