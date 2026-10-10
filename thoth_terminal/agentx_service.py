"""AgentX workbench over immutable Thoth datasets; no separate model store."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from thoth_terminal.dataset_parts.mutations import (
    create_dataset, latest_dataset_version, publish_dataset_snapshot, replace_dataset_draft,
)
from thoth_terminal.models import Dataset, DatasetRow, DatasetVersion, Document

AGENTX_CONFIGURATION_PREFIX = "agentx-configuration-"
_CONFIG_KEY = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")


def get_agentx_schema(mode: str = "canonical") -> dict:
    if mode not in {"canonical", "theoretical"}:
        raise ValueError("mode must be canonical or theoretical")
    filename = "agentx_model.md" if mode == "canonical" else "agentx_theory_schema.md"
    return {"mode": mode, "schema_version": 1,
            "documentation_md": (Path(__file__).resolve().parent.parent / "docs" / filename).read_text(encoding="utf-8")}


def _agentx_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _agentx_version(session: Session, slug: str, version_id: int | None = None) -> DatasetVersion | None:
    dataset = session.query(Dataset).filter_by(slug=slug).first()
    if dataset is None:
        if version_id is not None:
            raise ValueError(f"Dataset {slug} is missing")
        return None
    if version_id is None:
        return latest_dataset_version(session, dataset.id, status="published")
    version = session.get(DatasetVersion, version_id)
    if version is None or version.dataset_id != dataset.id or version.status != "published":
        raise ValueError(f"{slug} requires an exact published version")
    return version


def _agentx_rows(session: Session, version: DatasetVersion, *, limit: int, model: str | None = None) -> tuple[list[dict], int]:
    query = session.query(DatasetRow).filter_by(dataset_version_id=version.id)
    if model:
        query = query.filter(DatasetRow.values["model"].as_string() == model)
    total = query.count()
    rows = query.order_by(DatasetRow.sort_order, DatasetRow.id).limit(limit).all()
    return [dict(row.values or {}, source_dataset_version_id=version.id, source_dataset_row_id=row.id) for row in rows], total


def agentx_context(session: Session) -> dict:
    benchmarks = _agentx_version(session, "inferencex-upstream-benchmarks")
    models = []
    model_options = []
    if benchmarks:
        model_expr = DatasetRow.values["model"].as_string()
        models = [row[0] for row in session.query(model_expr).filter_by(dataset_version_id=benchmarks.id)
                  .filter(DatasetRow.values["benchmark_type"].as_string() == "agentic_traces")
                  .distinct().order_by(model_expr).limit(100).all() if row[0]]
        display_expr = DatasetRow.values["display_model"].as_string()
        model_options = [{"source_model": row[0], "display_model": row[1] or row[0]}
                         for row in session.query(model_expr, display_expr).filter_by(dataset_version_id=benchmarks.id)
                         .filter(DatasetRow.values["benchmark_type"].as_string() == "agentic_traces")
                         .distinct().order_by(model_expr).limit(100).all() if row[0]]
    configs = session.query(Dataset.id, Dataset.slug, Dataset.title).filter(
        Dataset.source_kind == "agentx_configuration").order_by(Dataset.updated_at.desc()).limit(100).all()
    return {
        "schema_version": 1, "models": models, "model_options": model_options,
        "benchmark_version_id": benchmarks.id if benchmarks else None,
        "benchmark_metadata": dict(benchmarks.extra or {}) if benchmarks else {},
        "configurations": [{"dataset_id": row.id, "configuration_key": row.slug.removeprefix(AGENTX_CONFIGURATION_PREFIX),
                            "name": row.title} for row in configs],
        "configuration_limit": 100,
        "configuration_count": session.query(func.count(Dataset.id)).filter(Dataset.source_kind == "agentx_configuration").scalar(),
    }


def _agentx_evidence(session: Session, payload: dict) -> tuple[list[dict], dict]:
    raw = payload.get("evidence_catalog", [])
    version_id = payload.get("evidence_version_id")
    if version_id is not None:
        if raw:
            raise ValueError("Choose inline evidence or a pinned evidence dataset, not both")
        version = session.get(DatasetVersion, int(version_id))
        if version is None or version.status != "published":
            raise ValueError("Evidence must pin a published dataset version")
        raw, total = _agentx_rows(session, version, limit=500)
        if total > 500:
            raise ValueError("Evidence catalogue exceeds 500 entries; select a bounded catalogue")
        lineage = {"evidence_dataset_version_id": version.id}
    else:
        lineage = {"evidence_basis": "explicit_supplied_catalogue"}
    if not isinstance(raw, list) or len(raw) > 500 or any(not isinstance(item, dict) for item in raw):
        raise ValueError("evidence_catalog must contain at most 500 evidence objects")
    ids: set[int] = set()
    for item in raw:
        refs = item.get("source_document_ids", [])
        if not isinstance(refs, list) or any(type(ref) is not int or ref < 1 for ref in refs):
            raise ValueError("Evidence source_document_ids must be positive stored document IDs")
        ids.update(refs)
    if ids and {row[0] for row in session.query(Document.id).filter(Document.id.in_(ids)).all()} != ids:
        raise ValueError("An evidence source document does not exist")
    lineage["evidence_digest"] = _agentx_digest(raw)
    lineage["source_document_ids"] = sorted(ids)
    return raw, lineage


def evaluate_agentx_configuration(session: Session, payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("An AgentX configuration object is required")
    if len(json.dumps(payload, allow_nan=False).encode()) > 256_000:
        raise ValueError("AgentX configuration exceeds 256 KB")
    mode = payload.get("mode")
    configuration = payload.get("configuration")
    if not isinstance(configuration, dict):
        raise ValueError("configuration must be an object")
    if mode == "canonical":
        from thoth_terminal.agentx_economics import evaluate_agentx_economics
        model = str(payload.get("model") or "").strip()
        if not model:
            raise ValueError("Select one exact benchmark model before evaluation")
        version = _agentx_version(session, "inferencex-upstream-benchmarks", payload.get("benchmark_version_id"))
        if version is None:
            return {"mode": mode, "status": "missing_inputs", "gaps": ["No published AgentX benchmark snapshot"], "results": []}
        rows, total = _agentx_rows(session, version, limit=5000, model=model)
        if total > 5000:
            raise ValueError("Selected model exceeds 5000 rows; select a smaller published benchmark dataset")
        result = evaluate_agentx_economics(rows, configuration)
        return dict(result, mode=mode, source_lineage={"benchmark_dataset_version_id": version.id,
                    "model": model, "rows_read": len(rows), "rows_available": total,
                    "configuration_digest": _agentx_digest(configuration)})
    if mode != "theoretical":
        raise ValueError("mode must be canonical or theoretical")
    from thoth_terminal.agentx_theory import evaluate_agentx_theory
    evidence, lineage = _agentx_evidence(session, payload)
    result = evaluate_agentx_theory(configuration, evidence)
    result = dict(result, mode=mode, source_lineage=dict(lineage, configuration_digest=_agentx_digest(configuration)))
    economics = payload.get("economics")
    if economics is not None:
        if not isinstance(economics, dict):
            raise ValueError("economics must be an object")
        if result.get("economics_ready"):
            from thoth_terminal.agentx_economics import evaluate_agentx_capacity_economics
            bounds = result["bounds"]["per_gpu_tokens_per_second_upper"]
            result["economic_capacity_bounds"] = {
                endpoint: evaluate_agentx_capacity_economics(bounds[endpoint], economics)
                for endpoint in ("low", "high")
            }
            result["economic_capacity_note"] = "Resource upper bounds under supplied assumptions; neither measured throughput nor a demand or profit forecast."
        else:
            result["economic_capacity_bounds"] = None
    return result


def save_agentx_configuration(session: Session, payload: dict) -> dict:
    key = str(payload.get("configuration_key") or "")
    if not _CONFIG_KEY.fullmatch(key):
        raise ValueError("configuration_key needs 2-64 lowercase letters, digits, underscores or hyphens")
    summary = str(payload.get("change_summary") or "").strip()
    if not summary or len(summary) > 4000:
        raise ValueError("A change_summary of at most 4000 characters is required")
    document_ids = payload.get("source_document_ids", [])
    if not isinstance(document_ids, list) or len(document_ids) > 100 or any(type(item) is not int or item < 1 for item in document_ids):
        raise ValueError("source_document_ids must contain at most 100 positive stored IDs")
    if document_ids and {row[0] for row in session.query(Document.id).filter(Document.id.in_(document_ids)).all()} != set(document_ids):
        raise ValueError("A source document does not exist")
    slug = AGENTX_CONFIGURATION_PREFIX + key
    # FOR UPDATE cannot lock an absent row. Serialize exact identities on the
    # live Postgres runtime before the first lookup, including initial saves.
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": slug})
    dataset = session.query(Dataset).filter_by(slug=slug).with_for_update().first()
    if dataset is not None and dataset.source_kind != "agentx_configuration":
        raise ValueError("Configuration key belongs to a different dataset")
    previous = latest_dataset_version(session, dataset.id, status="published") if dataset else None
    expected = payload.get("expected_version_id")
    if (previous and expected != previous.id) or (not previous and expected is not None):
        raise ValueError("Configuration changed; reload its published version before saving")
    result = evaluate_agentx_configuration(session, payload)
    if payload["mode"] == "canonical" and not result.get("source_lineage", {}).get("benchmark_dataset_version_id"):
        raise ValueError("Publish an AgentX benchmark snapshot before saving a canonical configuration")
    pinned = {k: payload[k] for k in ("mode", "model", "configuration", "evidence_catalog", "evidence_version_id", "economics") if k in payload}
    if payload["mode"] == "canonical" and result.get("source_lineage"):
        pinned["benchmark_version_id"] = result["source_lineage"]["benchmark_dataset_version_id"]
    ancestor = payload.get("supersedes")
    if ancestor is not None:
        if not isinstance(ancestor, dict) or not _CONFIG_KEY.fullmatch(str(ancestor.get("configuration_key") or "")):
            raise ValueError("supersedes requires a configuration_key and exact version_id")
        _agentx_version(session, AGENTX_CONFIGURATION_PREFIX + ancestor["configuration_key"], int(ancestor["version_id"]))
    record = {"configuration_key": key, "mode": payload["mode"], "status": result.get("status"),
              "configuration": pinned, "evaluation": result, "change_summary": summary,
              "previous_version_id": previous.id if previous else None, "supersedes": ancestor}
    record["source_document_ids"] = document_ids
    columns = [{"key": name, "label": name.replace("_", " ").title(), "data_type": "json" if name in {"configuration", "evaluation", "supersedes", "source_document_ids"} else "number" if name == "previous_version_id" else "text"}
               for name in record]
    rows = [{"row_key": key, "values": record}]
    extra = {"model_schema_version": 1, "source_lineage": result.get("source_lineage", {}), "change_summary": summary}
    if dataset is None:
        dataset = create_dataset(session, title=str(payload.get("name") or key), slug=slug,
                                 family="ai_serving_economics", category="ai_infrastructure", source_kind="agentx_configuration",
                                 columns=columns, rows=rows, extra=extra, source_event="agentx_configuration_save")
        if dataset.slug != slug:
            raise ValueError("Configuration changed; reload its published version before saving")
    else:
        replace_dataset_draft(session, dataset, columns=columns, rows=rows, extra=extra, source_event="agentx_configuration_save")
    published = publish_dataset_snapshot(session, dataset, label=summary[:64])
    return {"configuration_key": key, "dataset_id": dataset.id, "version_id": published.id,
            "version": published.version, "previous_version_id": previous.id if previous else None,
            "url": f"/agentx?configuration_key={key}", "evaluation": result}


def get_agentx_configuration(session: Session, key: str, version_id: int | None = None) -> dict:
    if not _CONFIG_KEY.fullmatch(key):
        raise ValueError("Invalid configuration_key")
    version = _agentx_version(session, AGENTX_CONFIGURATION_PREFIX + key, version_id)
    if version is None:
        raise ValueError("AgentX configuration not found")
    rows, _ = _agentx_rows(session, version, limit=1)
    history = session.query(DatasetVersion.id, DatasetVersion.version, DatasetVersion.label).filter_by(
        dataset_id=version.dataset_id, status="published").order_by(DatasetVersion.version.desc()).limit(100).all()
    return {"version_id": version.id, "version": version.version, "record": rows[0],
            "history": [{"version_id": row.id, "version": row.version, "change_summary": row.label} for row in history]}


def apply_worldview_agentx_configuration(session: Session, *, cloud_run_id: int, capability: str,
                                        operation_key: str, payload: dict) -> dict:
    from thoth_terminal.cloud_goal_run_service import append_cloud_goal_event
    from thoth_terminal.worldview_write_service import _authorized_run, _replayed_receipt
    if not 1 <= len(str(operation_key or "").strip()) <= 120:
        raise ValueError("operation_key must contain 1-120 characters")
    run = _authorized_run(session, cloud_run_id=cloud_run_id, capability=capability)
    replay = _replayed_receipt(session, cloud_run_id=cloud_run_id, event_kind="worldview_agentx_configuration_write", operation_key=operation_key)
    if replay is not None:
        return replay
    receipt = save_agentx_configuration(session, payload)
    receipt["replayed"] = False
    append_cloud_goal_event(session, run, event_kind="worldview_agentx_configuration_write",
                           summary=payload["change_summary"], payload={"operation_key": operation_key, "receipt": receipt})
    return receipt
