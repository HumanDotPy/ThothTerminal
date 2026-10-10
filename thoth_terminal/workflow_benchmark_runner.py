from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from thoth_terminal.workflow_benchmark import validate_workflow_benchmark_manifest


RUNNABLE_STAGES = {
    "long_context_fidelity": "signal_extraction",
    "compression_recovery": "signal_extraction",
    "multimodal_provenance": "media_route",
    "cross_document_reconciliation": "signal_extraction",
    "durable_write_safety": "durable_preview",
    "source_grounded_knowledge": "knowledge_qa",
    "negative_media_rejection": "media_route",
}


def load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def build_evaluation_plan(
    *,
    slate: dict[str, Any],
    manifests: list[dict[str, Any]],
    configurations: list[dict[str, Any]],
    structured_cases_by_file: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    errors = validate_evaluation_slate(slate)
    if errors:
        raise ValueError("Invalid evaluation slate: " + "; ".join(errors))
    if not configurations:
        raise ValueError("At least one candidate configuration is required")

    manifest_by_key: dict[str, dict[str, Any]] = {}
    for manifest in manifests:
        manifest_errors = validate_workflow_benchmark_manifest(manifest)
        if manifest_errors:
            raise ValueError(f"Invalid workflow manifest {manifest.get('case_key')}: " + "; ".join(manifest_errors))
        manifest_by_key[str(manifest["case_key"])] = manifest

    cells: list[dict[str, Any]] = []
    for track in slate["tracks"]:
        track_key = str(track["track_key"])
        stage = RUNNABLE_STAGES.get(track_key, "planned")
        if track.get("structured_case_file"):
            structured_file = str(track["structured_case_file"])
            questions = (structured_cases_by_file or {}).get(structured_file, [])
            if not questions:
                questions = [{"case_key": structured_file, "context_tier": None}]
            for question in questions:
                for mode in track.get("modes", []):
                    for tier in track.get("context_tiers", []):
                        declared_tier = str(question.get("context_tier") or "")
                        status = "ready_to_prepare" if declared_tier == str(tier) else "blocked_missing_payload"
                        for config in configurations:
                            cells.append(
                                _cell(
                                    track=track,
                                    case_key=str(question.get("case_key") or structured_file),
                                    variant_key=str(tier),
                                    mode=str(mode),
                                    stage=stage,
                                    status=status,
                                    config=config,
                                )
                            )
            continue

        for case_key in track.get("case_keys", []):
            manifest = manifest_by_key.get(str(case_key))
            available = {
                str(row.get("variant_key"))
                for row in (manifest or {}).get("evaluation_variants", [])
                if isinstance(row, dict)
            }
            for variant_key in track.get("variant_keys", []):
                status = "ready_to_prepare"
                if manifest is None:
                    status = "blocked_missing_case"
                elif str(variant_key) not in available:
                    status = "blocked_missing_variant"
                for config in configurations:
                    cells.append(
                        _cell(
                            track=track,
                            case_key=str(case_key),
                            variant_key=str(variant_key),
                            mode=None,
                            stage=stage,
                            status=status,
                            config=config,
                        )
                    )

    counts: dict[str, int] = {}
    for cell in cells:
        counts[cell["status"]] = counts.get(cell["status"], 0) + 1
    return {
        "slate_key": slate["slate_key"],
        "plan_fingerprint": _fingerprint({"slate": slate, "configurations": configurations, "cells": cells}),
        "configuration_count": len(configurations),
        "cell_count": len(cells),
        "status_counts": counts,
        "cells": cells,
    }


def validate_evaluation_slate(slate: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not str(slate.get("slate_key") or "").strip():
        errors.append("slate_key is required")
    tracks = slate.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        return errors + ["tracks must contain at least one track"]
    seen: set[str] = set()
    for index, track in enumerate(tracks):
        if not isinstance(track, dict):
            errors.append(f"tracks[{index}] must be an object")
            continue
        key = str(track.get("track_key") or "").strip()
        if not key:
            errors.append(f"tracks[{index}].track_key is required")
        elif key in seen:
            errors.append(f"tracks[{index}].track_key is duplicated")
        seen.add(key)
        if not track.get("case_keys") and not track.get("structured_case_file"):
            errors.append(f"tracks[{index}] requires case_keys or structured_case_file")
        if not track.get("variant_keys") and not track.get("context_tiers"):
            errors.append(f"tracks[{index}] requires variant_keys or context_tiers")
        if not track.get("primary_metrics"):
            errors.append(f"tracks[{index}].primary_metrics is required")
        if not track.get("hard_gates"):
            errors.append(f"tracks[{index}].hard_gates is required")
    return errors


def summarize_evaluation_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [row for row in results if row.get("status") == "completed"]
    accepted = [row for row in completed if row.get("accepted") is True]
    hard_gate_failures = [row for row in completed if row.get("hard_gate_passed") is False]
    total_cost = sum(float(row.get("cost_usd") or 0) for row in completed)
    total_latency = sum(float(row.get("latency_seconds") or 0) for row in completed)
    return {
        "result_count": len(results),
        "completed_count": len(completed),
        "accepted_count": len(accepted),
        "accepted_result_rate": len(accepted) / len(completed) if completed else None,
        "hard_gate_failure_count": len(hard_gate_failures),
        "cost_per_accepted_result": total_cost / len(accepted) if accepted else None,
        "latency_per_accepted_result_seconds": total_latency / len(accepted) if accepted else None,
        "tool_error_rate": (
            sum(1 for row in completed if row.get("tool_error")) / len(completed) if completed else None
        ),
        "escalation_rate": (
            sum(1 for row in completed if row.get("escalated")) / len(completed) if completed else None
        ),
    }


def _cell(
    *,
    track: dict[str, Any],
    case_key: str,
    variant_key: str,
    mode: str | None,
    stage: str,
    status: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    config_key = str(config.get("configuration_key") or "").strip()
    if not config_key:
        raise ValueError("Every candidate configuration requires configuration_key")
    identity = {
        "track_key": track["track_key"],
        "case_key": case_key,
        "variant_key": variant_key,
        "mode": mode,
        "configuration_key": config_key,
    }
    return {
        "cell_key": _fingerprint(identity)[:20],
        **identity,
        "execution_stage": stage,
        "status": status,
        "primary_metrics": list(track.get("primary_metrics") or []),
        "hard_gates": list(track.get("hard_gates") or []),
        "configuration": dict(config),
    }


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
