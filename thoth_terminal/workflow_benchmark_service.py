from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.models import LocalAgentEvalCase
from thoth_terminal.workflow_benchmark import canonical_manifest_fingerprint, validate_workflow_benchmark_manifest


HIDDEN_SPLITS = {"hidden_validation", "frozen_holdout"}


def import_workflow_benchmark_file(session: Session, *, path: str | Path) -> dict[str, int]:
    source_path = Path(path).expanduser().resolve(strict=True)
    payload = json.loads(source_path.read_text(encoding="utf-8-sig"))
    manifests = payload if isinstance(payload, list) else [payload]
    counts = {"parsed": len(manifests), "created": 0, "updated": 0}
    for manifest in manifests:
        result = upsert_workflow_benchmark_case(session, manifest=manifest, source_file=str(source_path))
        counts[result] += 1
    return counts


def upsert_workflow_benchmark_case(
    session: Session,
    *,
    manifest: dict[str, Any],
    source_file: str | None = None,
) -> str:
    errors = validate_workflow_benchmark_manifest(manifest)
    if errors:
        raise ValueError("invalid workflow benchmark manifest: " + "; ".join(errors))
    stored = copy.deepcopy(manifest)
    stored["manifest_fingerprint"] = canonical_manifest_fingerprint(stored)
    if source_file:
        stored["source_file"] = source_file
    case_key = str(stored["case_key"]).strip()
    slug = _case_slug(case_key)
    now = utcnow()
    row = session.query(LocalAgentEvalCase).filter_by(slug=slug).first()
    outcome = "created" if row is None else "updated"
    if row is None:
        row = LocalAgentEvalCase(slug=slug, title=case_key[:255], created_at=now, updated_at=now)
        session.add(row)
    artifacts = [item for item in stored.get("artifacts", []) if isinstance(item, dict)]
    document_ids = [int(item["document_id"]) for item in artifacts if item.get("document_id") is not None]
    row.title = str(stored.get("title") or case_key)[:255]
    row.description = str(stored.get("description") or "Source-packet workflow benchmark case.")
    row.source_label = "workflow_benchmark"
    row.document_id = document_ids[0] if len(document_ids) == 1 else None
    row.fixture_title = row.title
    row.fixture_text = None
    row.expected_json = {"eval_kind": "workflow_benchmark", "workflow_benchmark": stored}
    row.tags_json = sorted(
        {
            "workflow-benchmark",
            str(stored.get("primary_category")),
            str(stored.get("split")),
            *[str(tag) for tag in stored.get("tags", []) if str(tag).strip()],
        }
    )
    row.active = bool(stored.get("active", True))
    row.updated_at = now
    session.flush()
    return outcome


def serialize_workflow_benchmark_case(case: LocalAgentEvalCase, *, include_gold: bool = False) -> dict[str, Any]:
    manifest = copy.deepcopy((case.expected_json or {}).get("workflow_benchmark") or {})
    split = str(manifest.get("split") or "")
    if not include_gold or split in HIDDEN_SPLITS:
        manifest.pop("gold_items", None)
        manifest.pop("adjudicated_by", None)
        manifest.pop("adjudication_history", None)
        manifest.pop("source_file", None)
    return {
        "id": int(case.id),
        "slug": case.slug,
        "title": case.title,
        "document_id": case.document_id,
        "manifest": manifest,
        "gold_redacted": not include_gold or split in HIDDEN_SPLITS,
    }


def _case_slug(case_key: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", case_key.lower()).strip("-")
    return f"workflow-{value}"[:128].rstrip("-")
