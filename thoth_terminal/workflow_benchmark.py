from __future__ import annotations

import hashlib
import json
from typing import Any


WORKFLOW_CATEGORIES = {
    "short_text_signal",
    "long_form_text",
    "long_form_multimodal",
    "multi_document_reconciliation",
    "ocr_document",
    "visual_chart_table",
    "visual_semantic",
    "image_only_durable",
    "translation_multimodal",
    "compression_recovery",
    "context_saturation",
    "terminal_assisted_research",
    "durable_application",
}
BENCHMARK_SPLITS = {"development", "hidden_validation", "frozen_holdout", "challenge"}
GOLD_DISPOSITIONS = {"supported", "unsupported", "ambiguous"}
DURABLE_ACTIONS = {"write", "hold", "update", "supersede", "contradict", "dedupe", "no_write"}
VERIFICATION_STATES = {"candidate", "challenged", "verified", "disputed", "retired"}


def canonical_manifest_fingerprint(manifest: dict[str, Any]) -> str:
    """Return a stable revision fingerprint without trusting a supplied hash."""
    payload = dict(manifest or {})
    payload.pop("manifest_fingerprint", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_workflow_benchmark_manifest(manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(manifest, dict):
        return ["manifest must be an object"]

    case_key = _text(manifest.get("case_key"))
    if not case_key:
        errors.append("case_key is required")
    if _text(manifest.get("primary_category")) not in WORKFLOW_CATEGORIES:
        errors.append("primary_category is not recognized")
    if _text(manifest.get("split")) not in BENCHMARK_SPLITS:
        errors.append("split is not recognized")
    state = _text(manifest.get("verification_state")) or "candidate"
    if state not in VERIFICATION_STATES:
        errors.append("verification_state is not recognized")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        errors.append("artifacts must contain at least one source artifact")
    else:
        for index, artifact in enumerate(artifacts):
            prefix = f"artifacts[{index}]"
            if not isinstance(artifact, dict):
                errors.append(f"{prefix} must be an object")
                continue
            if not _text(artifact.get("artifact_key")):
                errors.append(f"{prefix}.artifact_key is required")
            digest = _text(artifact.get("sha256"))
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
                errors.append(f"{prefix}.sha256 must be a 64-character hex digest")
            if not any(artifact.get(key) not in (None, "") for key in ("document_id", "attachment_id", "source_url", "object_ref")):
                errors.append(f"{prefix} requires a stable Thoth reference or source_url")

    gold_items = manifest.get("gold_items")
    if not isinstance(gold_items, list) or not gold_items:
        errors.append("gold_items must contain at least one item")
    else:
        seen: set[str] = set()
        for index, item in enumerate(gold_items):
            prefix = f"gold_items[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{prefix} must be an object")
                continue
            gold_id = _text(item.get("gold_id"))
            if not gold_id:
                errors.append(f"{prefix}.gold_id is required")
            elif gold_id in seen:
                errors.append(f"{prefix}.gold_id is duplicated")
            seen.add(gold_id)
            disposition = _text(item.get("disposition"))
            if disposition not in GOLD_DISPOSITIONS:
                errors.append(f"{prefix}.disposition is not recognized")
            action = _text(item.get("durable_action")) or "no_write"
            if action not in DURABLE_ACTIONS:
                errors.append(f"{prefix}.durable_action is not recognized")
            anchors = item.get("anchors")
            if not isinstance(anchors, list) or not anchors:
                errors.append(f"{prefix}.anchors must contain source evidence")
            elif not all(isinstance(anchor, dict) and _text(anchor.get("artifact_key")) for anchor in anchors):
                errors.append(f"{prefix}.anchors require artifact_key values")
            if disposition == "supported" and not _text(item.get("proposition")):
                errors.append(f"{prefix}.proposition is required for supported gold")
            if not _text(item.get("source_intent")):
                errors.append(f"{prefix}.source_intent is required")
            if state == "verified" and not _text(item.get("verified_by")):
                errors.append(f"{prefix}.verified_by is required for a verified manifest")

    variants = manifest.get("evaluation_variants")
    if not isinstance(variants, list) or not variants:
        errors.append("evaluation_variants must contain at least one context/compression variant")
    elif not all(isinstance(row, dict) and _text(row.get("variant_key")) for row in variants):
        errors.append("every evaluation variant requires variant_key")

    if state == "verified" and not _text(manifest.get("adjudicated_by")):
        errors.append("adjudicated_by is required for a verified manifest")
    return errors


def _text(value: Any) -> str:
    return str(value or "").strip()
