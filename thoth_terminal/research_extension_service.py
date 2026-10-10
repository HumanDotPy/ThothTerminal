"""Versioned research extensions, isolated calculator replays and audit receipts.

Calculator code runs only through the isolated executor. The caller owns the
transaction; all transitions and receipts flush together.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.models import ResearchExtension, ResearchExtensionEvent, ResearchExtensionVersion
from thoth_terminal.research_extension_executor import canonical_json, execute_calculator, get_execution_image_id, validate_execution_spec
from thoth_terminal.worldview_write_service import _authorized_run


_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9-]{2,95}$")
_ALLOWED_KINDS = {"skill", "plugin"}
_MAX_CONTENT_CHARS = 40_000
_MAX_MANIFEST_CHARS = 12_000
_MAX_EXECUTABLE_MANIFEST_BYTES = 256 * 1024
_MAX_CASE_JSON_BYTES = 64 * 1024
_SECRET_RE = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/-]{16,}|"
    r"(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*[\"']?[A-Za-z0-9._~+/-]{16,}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)"
)
_FORBIDDEN_MANIFEST_KEYS = {
    "code", "python", "javascript", "script", "shell", "command", "entrypoint",
    "module", "package", "executable", "subprocess", "filesystem", "write_path",
    "install", "pip", "npm", "postinstall", "hooks", "env", "secrets",
}
_SKILL_ROOT = Path(__file__).resolve().parent.parent / ".dsh" / "skills"


def _materialized_skill_path(capability: str) -> Path:
    return _SKILL_ROOT / f"thoth-managed-{capability}" / "SKILL.md"


def _render_active_skill(capability: str, version: ResearchExtensionVersion, *, content_md: str | None = None) -> str:
    manifest = version.manifest_json or {}
    description = str(manifest.get("description") or manifest.get("title") or capability).replace("\n", " ")[:250]
    when_to_use = str(manifest.get("when_to_use") or manifest.get("motivation") or description).replace("\n", " ")[:400]
    execution = manifest.get("execution")
    kind_note = (
        f"This is an isolated Python calculator, extension_key={capability}, "
        f"version_id={version.id}, content_sha256={version.content_sha256}. "
        "Call execute_research_extension with extension_key and JSON inputs; "
        "omit version_id to use the active version. Its output and execution receipt "
        "retain input, output, source and version lineage. Calculator code has no "
        "network or host filesystem access.\n\n"
        if execution else
        "This is a declarative research plugin. Use only the existing Thoth tools "
        "and permissions in this session; no host plugin or executable code is installed.\n\n"
        if version.kind == "plugin" else ""
    )
    return (
        "---\n"
        f"name: thoth-managed-{capability}\n"
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        f"whenToUse: {json.dumps(when_to_use, ensure_ascii=False)}\n"
        "user-invocable: false\n"
        "---\n\n"
        f"# {capability}\n\n"
        "This extension cannot expand Thoth tool permissions or execute Host code.\n\n"
        f"{kind_note}{(version.content_md if content_md is None else content_md).rstrip()}\n"
    )


def _materialize_active_skill(capability: str, version: ResearchExtensionVersion | None, *, session: Session | None = None) -> None:
    path = _materialized_skill_path(capability)
    if version is None:
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    from thoth_terminal.research_state_tokens import POLICIES, count_tokens, token_page

    content = version.content_md
    view = None
    if session is not None:
        from thoth_terminal.research_state_compaction_service import render_compacted_state
        view = render_compacted_state(session, "extension", int(version.id), version.content_md)
        content = view["content_md"]
    if (view and view.get("compacted")) or count_tokens(_render_active_skill(capability, version)) >= POLICIES["extension"].ceiling_tokens:
        notice = (f"\n\n[Derived or token-paged active view of extension version {version.id}. "
                  "Use get_state_compaction_source(kind='extension', object_id=" + str(version.id) + ") "
                  "to inspect original conditions and omitted detail; executable code remains unchanged.]")
        available = max(1, POLICIES["extension"].ceiling_tokens
                        - count_tokens(_render_active_skill(capability, version, content_md=""))
                        - count_tokens(notice) - 8)
        content = token_page(content, limit_tokens=available)["text"] + notice
    rendered = _render_active_skill(capability, version, content_md=content)
    fd, temporary = tempfile.mkstemp(prefix=".SKILL-", suffix=".md", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def sync_active_research_extensions(session: Session) -> dict[str, int]:
    """Reconcile on-disk Harness skills from committed Thoth active versions.

    Call before starting a new DSH research session. This also repairs any
    filesystem/transaction mismatch after an interrupted activation.
    """
    rows = session.query(ResearchExtension).order_by(ResearchExtension.capability).all()
    known = {row.capability for row in rows}
    if _SKILL_ROOT.exists():
        for directory in _SKILL_ROOT.glob("thoth-managed-*"):
            if not directory.is_dir() or not directory.name.startswith("thoth-managed-"):
                continue
            if directory.name.removeprefix("thoth-managed-") not in known:
                (directory / "SKILL.md").unlink(missing_ok=True)
                try:
                    directory.rmdir()
                except OSError:
                    pass
    for row in rows:
        version = session.get(ResearchExtensionVersion, row.active_version_id) if row.active_version_id else None
        if version is not None and version.extension_id != row.id:
            raise ValueError(f"invalid active version for {row.capability}")
        if version is not None:
            _assert_version_integrity(session, version)
        _materialize_active_skill(row.capability, version, session=session)
    return {"extensions": len(rows), "active": sum(row.active_version_id is not None for row in rows)}


def _normalized_capability(value: str) -> str:
    capability = str(value or "").strip().lower()
    if not _CAPABILITY_RE.fullmatch(capability):
        raise ValueError("capability must be a lower-case slug (3–96 characters)")
    return capability


def _operation_key(value: str) -> str:
    key = str(value or "").strip()
    if not key or len(key) > 160:
        raise ValueError("operation_key must contain 1–160 characters")
    return key


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _bounded_json(value: object, *, label: str, maximum: int = _MAX_CASE_JSON_BYTES) -> str:
    def check(item: object) -> None:
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child)
            return
        raise ValueError(f"{label} must contain finite JSON values")
    try:
        check(value)
    except (RecursionError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain finite JSON values") from exc
    try:
        encoded = canonical_json(value, max_bytes=maximum).decode("utf-8")
    except ValueError as exc:
        raise ValueError(f"{label}: {exc}") from exc
    return encoded


def _json_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _outputs_match(actual: object, expected: object) -> bool:
    if type(actual) in (int, float) and type(expected) in (int, float):
        try:
            return math.isclose(actual, expected, abs_tol=1e-9, rel_tol=1e-6)
        except (OverflowError, ValueError):
            return actual == expected
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(_outputs_match(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(_outputs_match(a, b) for a, b in zip(actual, expected))
    return actual == expected


def _replay_cases(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise ValueError("executable plugin requires 1–8 replay_cases")
    seen: set[str] = set()
    for case in value:
        if not isinstance(case, dict) or set(case) - {"case_id", "inputs", "expected_output", "rationale", "source_document_ids"}:
            raise ValueError("replay case has unsupported fields")
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip() or len(case_id) > 160 or case_id in seen:
            raise ValueError("replay case_id must be a unique nonempty string of at most 160 characters")
        seen.add(case_id)
        if "inputs" not in case or "expected_output" not in case:
            raise ValueError("replay cases require inputs and expected_output")
        _bounded_json(case["inputs"], label="replay inputs")
        _bounded_json(case["expected_output"], label="expected_output")
        if "rationale" in case and (not isinstance(case["rationale"], str) or len(case["rationale"]) > 2000):
            raise ValueError("replay rationale must be text of at most 2000 characters")
        if "source_document_ids" in case and (
            not isinstance(case["source_document_ids"], list)
            or len(case["source_document_ids"]) > 100
            or any(type(item) is not int or item <= 0 for item in case["source_document_ids"])
        ):
            raise ValueError("source_document_ids must contain at most 100 positive document IDs")
    return value


def _content_digest(kind: str, content_md: str, manifest: dict[str, Any]) -> str:
    data = _canonical_json({"kind": kind, "content_md": content_md, "manifest": manifest})
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _manifest_has_executable_fields(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).strip().lower().replace("-", "_") in _FORBIDDEN_MANIFEST_KEYS
            or _manifest_has_executable_fields(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_manifest_has_executable_fields(item) for item in value)
    return False


def _candidate_payload(payload: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    kind = str(payload.get("kind") or "skill").strip().lower()
    if kind not in _ALLOWED_KINDS:
        raise ValueError("kind must be skill or plugin")
    content = payload.get("content_md", payload.get("instructions_md"))
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content_md must be nonempty Markdown")
    content = content.strip()
    if len(content) > _MAX_CONTENT_CHARS:
        raise ValueError(f"content_md exceeds {_MAX_CONTENT_CHARS} characters")
    raw_manifest = payload.get("manifest") or {}
    if not isinstance(raw_manifest, dict):
        raise ValueError("manifest must be an object")
    manifest = dict(raw_manifest)
    for key in ("title", "motivation", "source_document_ids", "validation_case_ids", "replay_comparisons", "execution", "replay_cases"):
        if key in payload:
            manifest[key] = payload[key]
    execution = manifest.get("execution")
    if "execution" in manifest:
        if kind != "plugin":
            raise ValueError("execution is only supported for plugin candidates")
        manifest["execution"] = validate_execution_spec(execution)
        _replay_cases(manifest.get("replay_cases"))
    elif "replay_cases" in manifest:
        raise ValueError("replay_cases require an executable plugin")
    encoded = _bounded_json(manifest, label="manifest", maximum=_MAX_EXECUTABLE_MANIFEST_BYTES if execution else _MAX_MANIFEST_CHARS * 4)
    if not execution and len(encoded) > _MAX_MANIFEST_CHARS:
        raise ValueError(f"manifest exceeds {_MAX_MANIFEST_CHARS} characters")
    # The validated execution spec is the sole allowed executable field.
    declarative = {key: value for key, value in manifest.items() if key not in {"execution", "replay_cases"}}
    if _manifest_has_executable_fields(declarative):
        raise ValueError("executable plugin fields are outside the scoped research extension boundary")
    if _SECRET_RE.search(content) or _SECRET_RE.search(encoded):
        raise ValueError("candidate appears to contain a credential; remove it before creating the extension")
    return kind, content, manifest


def _version_view(row: ResearchExtensionVersion, *, include_content: bool = True) -> dict[str, Any]:
    view = {
        "candidate_id": row.id,
        "version": row.version,
        "kind": row.kind,
        "content_md": row.content_md,
        "manifest": row.manifest_json,
        "content_sha256": row.content_sha256,
        "validation": row.validation_json,
        "validated_at": row.validated_at.isoformat() if row.validated_at else None,
        "created_by_run_id": row.created_by_run_id,
        "created_at": row.created_at.isoformat(),
    }
    if not include_content:
        view.pop("content_md")
        view.pop("manifest")
        view["executable"] = bool((row.manifest_json or {}).get("execution"))
        view["validation"] = {key: value for key, value in (row.validation_json or {}).items() if key not in {"cases", "errors"}}
    return view


def _extension_view(session: Session, row: ResearchExtension, *, include_versions: bool = True) -> dict[str, Any]:
    versions = (
        session.query(ResearchExtensionVersion)
        .filter(ResearchExtensionVersion.extension_id == row.id)
        .order_by(ResearchExtensionVersion.version.desc())
        .limit(5 if include_versions else 20)
        .all()
    )
    recent = get_research_extension_executions(session, extension_key=row.capability, limit=5 if include_versions else 3)
    if not include_versions:
        recent = [{key: value for key, value in event.items() if key not in {"inputs", "execution"}} for event in recent]
    return {
        "capability": row.capability,
        "kind": row.kind,
        "active_version_id": row.active_version_id,
        "versions": [_version_view(version, include_content=include_versions) for version in versions],
        "recent_executions": recent,
        "updated_at": row.updated_at.isoformat(),
    }


def _event_receipt(event: ResearchExtensionEvent) -> dict[str, Any]:
    return {
        "event_id": event.id,
        "operation_key": event.operation_key,
        "action": event.action,
        "candidate_id": event.candidate_id,
        "prior_version_id": event.prior_version_id,
        "active_version_id": event.active_version_id,
        "cloud_run_id": event.cloud_run_id,
        "created_at": event.created_at.isoformat(),
        **dict(event.receipt_json or {}),
    }


def _existing_receipt(session: Session, *, key: str, action: str, capability: str) -> dict[str, Any] | None:
    event = session.query(ResearchExtensionEvent).filter_by(operation_key=key).one_or_none()
    if event is None:
        return None
    extension = session.get(ResearchExtension, event.extension_id)
    if event.action != action or extension is None or extension.capability != capability:
        raise ValueError("operation_key already belongs to a different extension transition")
    return _event_receipt(event)


def _append_event(
    session: Session, *, key: str, extension: ResearchExtension, action: str,
    cloud_run_id: int | None, candidate_id: int | None,
    prior_version_id: int | None, receipt: dict[str, Any],
) -> dict[str, Any]:
    event = ResearchExtensionEvent(
        operation_key=key, extension_id=extension.id, candidate_id=candidate_id,
        action=action, cloud_run_id=cloud_run_id, prior_version_id=prior_version_id,
        active_version_id=extension.active_version_id, receipt_json=receipt, created_at=utcnow(),
    )
    session.add(event)
    session.flush()
    return _event_receipt(event)


def list_research_extensions(
    session: Session, *, extension_key: str | None = None, limit: int = 100,
) -> list[dict[str, Any]]:
    query = session.query(ResearchExtension)
    if extension_key:
        query = query.filter(ResearchExtension.capability == _normalized_capability(extension_key))
    rows = query.order_by(ResearchExtension.updated_at.desc()).limit(max(1, min(int(limit), 200))).all()
    return [_extension_view(session, row, include_versions=bool(extension_key)) for row in rows]


def create_research_extension_candidate(
    session: Session, *, cloud_run_id: int, capability: str,
    operation_key: str, payload: dict[str, Any],
) -> dict[str, Any]:
    _authorized_run(session, cloud_run_id=cloud_run_id, capability=capability)
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    extension_key = _normalized_capability(str(payload.get("extension_key") or ""))
    key = _operation_key(operation_key)
    prior = _existing_receipt(session, key=key, action="create", capability=extension_key)
    if prior is not None:
        return prior
    kind, content, manifest = _candidate_payload(payload)
    extension = (
        session.query(ResearchExtension)
        .filter_by(capability=extension_key)
        .with_for_update()
        .one_or_none()
    )
    now = utcnow()
    if extension is None:
        extension = ResearchExtension(
            capability=extension_key, kind=kind, active_version_id=None,
            created_at=now, updated_at=now,
        )
        session.add(extension)
        session.flush()
    elif extension.kind != kind:
        raise ValueError("candidate kind cannot change an existing capability")
    latest = (
        session.query(ResearchExtensionVersion.version)
        .filter_by(extension_id=extension.id)
        .order_by(ResearchExtensionVersion.version.desc())
        .first()
    )
    version = ResearchExtensionVersion(
        extension_id=extension.id, version=(latest[0] + 1 if latest else 1),
        kind=kind, content_md=content, manifest_json=manifest,
        content_sha256=_content_digest(kind, content, manifest),
        validation_json={}, validated_at=None, created_by_run_id=cloud_run_id, created_at=now,
    )
    session.add(version)
    extension.updated_at = now
    session.flush()
    return _append_event(
        session, key=key, extension=extension, action="create", cloud_run_id=cloud_run_id,
        candidate_id=version.id, prior_version_id=extension.active_version_id,
        receipt={"extension_key": extension_key, "version": version.version, "content_sha256": version.content_sha256},
    )


def _get_candidate(session: Session, candidate_id: int) -> tuple[ResearchExtension, ResearchExtensionVersion]:
    version = session.get(ResearchExtensionVersion, int(candidate_id))
    if version is None:
        raise ValueError("unknown research extension candidate")
    extension = (
        session.query(ResearchExtension)
        .filter_by(id=version.extension_id)
        .with_for_update()
        .one_or_none()
    )
    if extension is None:
        raise ValueError("unknown research extension")
    return extension, version


def _assert_version_integrity(session: Session, version: ResearchExtensionVersion) -> None:
    digest = _content_digest(version.kind, version.content_md, version.manifest_json)
    created = session.query(ResearchExtensionEvent).filter_by(action="create", candidate_id=version.id).first()
    original = (created.receipt_json or {}).get("content_sha256") if created else None
    if digest != version.content_sha256 or (original is not None and digest != original):
        raise ValueError("candidate content changed after creation")


def _executed_validation(
    session: Session, extension: ResearchExtension, version: ResearchExtensionVersion,
    manifest: dict[str, Any], errors: list[str],
) -> dict[str, Any]:
    baseline = session.get(ResearchExtensionVersion, extension.active_version_id) if extension.active_version_id else None
    baseline_spec = None
    baseline_error = None
    if baseline is not None:
        try:
            if baseline.extension_id != extension.id:
                raise ValueError("active baseline does not belong to extension")
            _assert_version_integrity(session, baseline)
            if not (baseline.manifest_json or {}).get("execution"):
                raise ValueError("active baseline is not executable")
            baseline_spec = validate_execution_spec(baseline.manifest_json["execution"])
        except ValueError as exc:
            baseline_error = str(exc)
    cases = []
    execution_image_id = None
    corrected = extended = regressions = 0
    replay_cases = list(manifest.get("replay_cases") or [])
    if baseline_spec is not None and not errors:
        candidate_cases = {case["case_id"]: case for case in replay_cases}
        for previous in (baseline.manifest_json or {}).get("replay_cases", []):
            replacement = candidate_cases.get(previous["case_id"])
            if replacement is None:
                replay_cases.append(previous)
            elif _json_digest(previous["inputs"]) != _json_digest(replacement["inputs"]):
                errors.append(f"replay case {previous['case_id']} inputs changed; use a new case_id and retain the prior input")
            elif _json_digest(previous["expected_output"]) != _json_digest(replacement["expected_output"]) and not str(replacement.get("rationale") or "").strip():
                errors.append(f"replay case {previous['case_id']} expected output changed without rationale")
        if len(replay_cases) > 16:
            errors.append("combined candidate and baseline replay cases exceed 16")
    if not errors:
        for case in replay_cases:
            inputs = case["inputs"]
            candidate = execute_calculator(manifest["execution"], inputs)
            repeat = execute_calculator(manifest["execution"], inputs)
            candidate_matches = candidate.get("ok") is True and _outputs_match(candidate.get("output"), case["expected_output"])
            # Hash outputs ourselves as well as retaining executor lineage; success
            # alone cannot establish deterministic behavior.
            deterministic = (
                candidate.get("ok") is True and repeat.get("ok") is True
                and _json_digest(candidate.get("output")) == _json_digest(repeat.get("output"))
                and candidate.get("output_sha256") == repeat.get("output_sha256")
            )
            baseline_receipt = execute_calculator(baseline_spec, inputs) if baseline_spec else None
            receipts = [candidate, repeat] + ([baseline_receipt] if baseline_receipt else [])
            case_image_ids = {receipt.get("image_id") for receipt in receipts}
            same_image = len(case_image_ids) == 1 and None not in case_image_ids
            if same_image:
                image_id = next(iter(case_image_ids))
                same_image = execution_image_id is None or image_id == execution_image_id
                execution_image_id = execution_image_id or image_id
            if not same_image:
                errors.append(f"execution image changed or was unavailable for replay case {case['case_id']}")
            baseline_matches = (
                baseline_receipt.get("ok") is True
                and _outputs_match(baseline_receipt.get("output"), case["expected_output"])
            ) if baseline_receipt is not None else None
            classification = "contract_case"
            if baseline_receipt is not None:
                if baseline_matches and not candidate_matches:
                    regressions += 1
                    classification = "regression"
                elif baseline_matches and candidate_matches:
                    classification = "unchanged_expected_behavior"
                elif candidate_matches and baseline_receipt.get("ok") is True:
                    corrected += 1
                    classification = "expected_output_failure_corrected"
                elif candidate_matches and baseline_receipt.get("error_type") == "calculator_error" and str(case.get("rationale") or "").strip():
                    extended += 1
                    classification = "supported_input_extension"
                else:
                    classification = "unresolved_case"
                if baseline_receipt.get("ok") is not True and baseline_receipt.get("error_type") != "calculator_error":
                    baseline_error = "baseline execution unavailable: " + str(baseline_receipt.get("error_type") or "unknown_failure")
            cases.append({
                "case_id": case["case_id"], "inputs": inputs,
                "input_sha256": _json_digest(inputs),
                "expected_output": case["expected_output"],
                "expected_output_sha256": _json_digest(case["expected_output"]),
                "rationale": case.get("rationale"), "source_document_ids": case.get("source_document_ids", []),
                "candidate": candidate, "candidate_repeat": repeat, "baseline": baseline_receipt,
                "candidate_matches_expected": candidate_matches,
                "baseline_matches_expected": baseline_matches,
                "deterministic": deterministic, "same_execution_image": same_image, "classification": classification,
            })
    all_pass = bool(cases) and all(case["candidate_matches_expected"] and case["deterministic"] for case in cases)
    if cases and not all_pass:
        errors.append("candidate replay failed expected output or deterministic execution")
    initial = baseline is None
    eligible = not errors and not baseline_error and all_pass and (initial or (baseline_spec is not None and regressions == 0 and corrected + extended > 0))
    gap = None
    if not eligible:
        gap = baseline_error or "Executable adoption requires passing deterministic contract cases and, for revisions, a measured correction or supported-input extension without regressions."
    return {
        "passed": not errors and all_pass, "adoption_eligible": eligible,
        "comparison_provenance": "executed_contract_cases" if initial else ("executed_paired_replay" if baseline_spec and not baseline_error else "baseline_execution_unavailable"),
        "comparative_evidence": baseline_spec is not None and not baseline_error,
        "baseline_version_id": extension.active_version_id,
        "baseline_content_sha256": baseline.content_sha256 if baseline else None,
        "baseline_unavailable_reason": baseline_error,
        "validated_content_sha256": version.content_sha256,
        "execution_image_id": execution_image_id,
        "comparison_case_count": len(cases), "corrected_case_count": corrected,
        "extended_case_count": extended, "regression_case_count": regressions,
        "checks": ["bounded_content", "isolated_python_execution", "content_integrity", "expected_outputs", "determinism", "paired_inputs"],
        "cases": cases, "errors": errors, "adoption_gap": gap,
    }


def validate_research_extension_candidate(
    session: Session, *, cloud_run_id: int, capability: str,
    operation_key: str, candidate_id: int,
) -> dict[str, Any]:
    _authorized_run(session, cloud_run_id=cloud_run_id, capability=capability)
    extension, version = _get_candidate(session, candidate_id)
    extension_key = extension.capability
    key = _operation_key(operation_key)
    prior = _existing_receipt(session, key=key, action="validate", capability=extension_key)
    if prior is not None:
        if prior["candidate_id"] != int(candidate_id):
            raise ValueError("operation_key was used for another candidate")
        return prior
    errors: list[str] = []
    manifest = version.manifest_json or {}
    try:
        kind, content, manifest = _candidate_payload({
            "kind": version.kind, "content_md": version.content_md, "manifest": version.manifest_json,
        })
        _assert_version_integrity(session, version)
    except ValueError as exc:
        errors.append(str(exc))
    # These comparisons are explicitly agent-reported; this function verifies
    # their shape, not their research truth. A trusted replay evaluator can
    # later supersede this provenance without changing the adoption ledger.
    comparisons = (version.manifest_json or {}).get("replay_comparisons")
    valid_comparisons = (
        isinstance(comparisons, list)
        and bool(comparisons)
        and all(
            isinstance(case, dict)
            and isinstance(case.get("case_id"), (str, int))
            and bool(str(case.get("baseline_outcome") or "").strip())
            and bool(str(case.get("candidate_outcome") or "").strip())
            and bool(str(case.get("improvement_reason") or "").strip())
            and case.get("candidate_better") is True
            for case in comparisons
        )
    )
    result = {
        "passed": not errors,
        "adoption_eligible": not errors and valid_comparisons,
        "comparison_provenance": "agent_reported_unverified" if valid_comparisons else "none",
        "comparison_case_count": len(comparisons) if isinstance(comparisons, list) else 0,
        "checks": ["bounded_content", "declarative_manifest", "content_integrity"],
        "errors": errors,
        "baseline_version_id": extension.active_version_id,
        "validated_content_sha256": version.content_sha256,
        "adoption_gap": None if valid_comparisons else "Provide comparable replay_comparisons with case_id, baseline_outcome, candidate_outcome, improvement_reason, and candidate_better=true.",
    }
    if "execution" in (version.manifest_json or {}):
        result = _executed_validation(session, extension, version, manifest, errors)
    version.validation_json = result
    version.validated_at = utcnow() if not errors else None
    session.flush()
    return _append_event(
        session, key=key, extension=extension, action="validate", cloud_run_id=cloud_run_id,
        candidate_id=version.id, prior_version_id=extension.active_version_id,
        receipt={"extension_key": extension_key, "validation": result, "content_sha256": version.content_sha256},
    )


def activate_research_extension_candidate(
    session: Session, *, cloud_run_id: int, capability: str,
    operation_key: str, candidate_id: int,
) -> dict[str, Any]:
    _authorized_run(session, cloud_run_id=cloud_run_id, capability=capability)
    extension, version = _get_candidate(session, candidate_id)
    extension_key = extension.capability
    key = _operation_key(operation_key)
    prior = _existing_receipt(session, key=key, action="activate", capability=extension_key)
    if prior is not None:
        if prior["candidate_id"] != int(candidate_id):
            raise ValueError("operation_key was used for another candidate")
        return prior
    if not version.validated_at or not (version.validation_json or {}).get("adoption_eligible"):
        raise ValueError("candidate needs structural validation and explicit comparative replay evidence before activation")
    _assert_version_integrity(session, version)
    if (version.validation_json or {}).get("validated_content_sha256") != version.content_sha256:
        raise ValueError("candidate content changed after validation")
    if (version.validation_json or {}).get("baseline_version_id") != extension.active_version_id:
        raise ValueError("active baseline changed after validation; validate against the current baseline")
    if (version.manifest_json or {}).get("execution"):
        if (version.validation_json or {}).get("execution_image_id") != get_execution_image_id():
            raise ValueError("execution image changed after validation; validate in the current environment")
    previous = extension.active_version_id
    extension.active_version_id = version.id
    extension.updated_at = utcnow()
    _materialize_active_skill(extension_key, version, session=session)
    return _append_event(
        session, key=key, extension=extension, action="activate", cloud_run_id=cloud_run_id,
        candidate_id=version.id, prior_version_id=previous,
        receipt={
            "extension_key": extension_key, "version": version.version,
            "content_sha256": version.content_sha256,
            "comparison_provenance": (version.validation_json or {}).get("comparison_provenance"),
            "comparison_case_count": (version.validation_json or {}).get("comparison_case_count"),
        },
    )


def rollback_research_extension(
    session: Session, *, cloud_run_id: int, capability: str,
    operation_key: str, extension_key: str, target_version_id: int,
) -> dict[str, Any]:
    _authorized_run(session, cloud_run_id=cloud_run_id, capability=capability)
    extension_key = _normalized_capability(extension_key)
    key = _operation_key(operation_key)
    prior = _existing_receipt(session, key=key, action="rollback", capability=extension_key)
    if prior is not None:
        requested = None if target_version_id == 0 else int(target_version_id)
        if prior["active_version_id"] != requested:
            raise ValueError("operation_key was used for another rollback target")
        return prior
    extension = (
        session.query(ResearchExtension)
        .filter_by(capability=extension_key)
        .with_for_update()
        .one_or_none()
    )
    if extension is None:
        raise ValueError("unknown research extension")
    previous = extension.active_version_id
    if previous is None:
        raise ValueError("extension has no active version to roll back")
    if target_version_id == 0:
        target_version_id = None
        target = None
    else:
        target = session.get(ResearchExtensionVersion, int(target_version_id))
        if target is None or target.extension_id != extension.id:
            raise ValueError("rollback target does not belong to extension")
        if not target.validated_at or not (target.validation_json or {}).get("passed"):
            raise ValueError("rollback target is not validated")
        _assert_version_integrity(session, target)
        activated_before = (
            session.query(ResearchExtensionEvent.id)
            .filter_by(extension_id=extension.id, action="activate", candidate_id=target.id)
            .first()
        )
        if activated_before is None:
            raise ValueError("rollback target was never activated")
    extension.active_version_id = target_version_id
    extension.updated_at = utcnow()
    _materialize_active_skill(extension_key, target, session=session)
    return _append_event(
        session, key=key, extension=extension, action="rollback", cloud_run_id=cloud_run_id,
        candidate_id=target_version_id, prior_version_id=previous,
        receipt={"extension_key": extension_key, "rolled_back_to": target_version_id},
    )


def get_research_extension_executions(
    session: Session, *, extension_key: str, limit: int = 20,
) -> list[dict[str, Any]]:
    """Read bounded execution receipts from the existing extension audit rail."""
    extension_key = _normalized_capability(extension_key)
    events = (
        session.query(ResearchExtensionEvent)
        .join(ResearchExtension, ResearchExtension.id == ResearchExtensionEvent.extension_id)
        .filter(ResearchExtension.capability == extension_key, ResearchExtensionEvent.action == "execute")
        .order_by(ResearchExtensionEvent.id.desc())
        .limit(max(1, min(int(limit), 30))).all()
    )
    return [_event_receipt(event) for event in events]


def execute_research_extension(
    session: Session, *, cloud_run_id: int, capability: str, operation_key: str,
    extension_key: str, inputs: Any, version_id: int | None = None,
) -> dict[str, Any]:
    """Execute an active or previously activated calculator and audit its result.

    This never installs code or writes research state. Candidate execution belongs
    to validation; a retained version is executable only after actual activation.
    """
    _authorized_run(session, cloud_run_id=cloud_run_id, capability=capability)
    extension_key = _normalized_capability(extension_key)
    key = _operation_key(operation_key)
    encoded_inputs = _bounded_json(inputs, label="inputs")
    input_sha256 = hashlib.sha256(encoded_inputs.encode("utf-8")).hexdigest()
    extension = session.query(ResearchExtension).filter_by(capability=extension_key).with_for_update().one_or_none()
    if extension is None:
        raise ValueError("unknown research extension")
    requested_version = int(version_id) if version_id is not None else extension.active_version_id
    if requested_version is None:
        raise ValueError("extension has no active calculator")
    prior = _existing_receipt(session, key=key, action="execute", capability=extension_key)
    if prior is not None:
        if prior["candidate_id"] != requested_version or prior.get("input_sha256") != input_sha256 or prior["cloud_run_id"] != cloud_run_id:
            raise ValueError("operation_key was used with different inputs, version or run")
        return {"ok": prior["execution"]["ok"], "output": prior["execution"].get("output"), "error": prior["execution"].get("error"), "receipt": prior}
    version = session.get(ResearchExtensionVersion, requested_version)
    if version is None or version.extension_id != extension.id:
        raise ValueError("execution version does not belong to extension")
    activated = session.query(ResearchExtensionEvent.id).filter_by(extension_id=extension.id, action="activate", candidate_id=version.id).first()
    if activated is None:
        raise ValueError("execution version was never activated; candidates run only during validation")
    _assert_version_integrity(session, version)
    spec = (version.manifest_json or {}).get("execution")
    if spec is None:
        raise ValueError("research extension version is not executable")
    spec = validate_execution_spec(spec)
    validated_image = (version.validation_json or {}).get("execution_image_id")
    if validated_image != get_execution_image_id():
        raise ValueError("execution image changed after validation; validate in the current environment")
    execution = execute_calculator(spec, inputs)
    if execution.get("image_id") is not None and execution["image_id"] != validated_image:
        execution = {**execution, "ok": False, "output": None,
                     "rejected_output_sha256": execution.get("output_sha256"), "output_sha256": None,
                     "error": "execution image changed during execution", "error_type": "environment_changed"}
    receipt = _append_event(
        session, key=key, extension=extension, action="execute", cloud_run_id=cloud_run_id,
        candidate_id=version.id, prior_version_id=extension.active_version_id,
        receipt={
            "extension_key": extension_key, "version_id": version.id, "version": version.version,
            "content_sha256": version.content_sha256, "source_sha256": execution.get("source_sha256"),
            "input_sha256": input_sha256, "inputs": inputs,
            "output_sha256": execution.get("output_sha256"), "execution": execution,
        },
    )
    return {"ok": execution["ok"], "output": execution.get("output"), "error": execution.get("error"), "receipt": receipt}
