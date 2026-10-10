from __future__ import annotations

from typing import Any

from thoth_terminal.agent.openshell import openshell_runtime_status
from thoth_terminal.config import settings


SAFE_LEDGER_WRITE_KINDS = {"cloud_goal_event", "cloud_goal", "daily_summary"}
DURABLE_WRITE_KINDS = {"report_addendum", "candidate_requests"}
WRITE_KIND_SETTINGS = {
    "report_addendum": "cloud_writes_allow_report_addendum",
    "candidate_requests": "cloud_writes_allow_candidate_requests",
}


class CloudWritePermissionError(RuntimeError):
    pass


def _text(value: object) -> str:
    return str(value or "").strip()


def _bool_setting(name: str, default: bool) -> bool:
    return bool(getattr(settings, name, default))


def cloud_write_policy_status(*, probe_openshell: bool = True, openshell_status: dict[str, Any] | None = None) -> dict[str, Any]:
    openshell = dict(openshell_status) if openshell_status is not None else openshell_runtime_status(probe_gateway=probe_openshell)
    durable_enabled = bool(_bool_setting("cloud_writes_enabled", True))
    require_openshell = bool(_bool_setting("cloud_writes_require_openshell", True))
    return {
        "enabled": durable_enabled,
        "require_openshell": require_openshell,
        "require_operator_approval": bool(_bool_setting("cloud_writes_require_operator_approval", True)),
        "allowed": {
            "cloud_goal_event": True,
            "cloud_goal": True,
            "daily_summary": True,
            "report_addendum": durable_enabled and _bool_setting("cloud_writes_allow_report_addendum", True),
            "candidate_requests": durable_enabled and _bool_setting("cloud_writes_allow_candidate_requests", True),
            "direct_report_version": durable_enabled and _bool_setting("cloud_writes_allow_direct_report_version", False),
        },
        "openshell": openshell,
        "openshell_ready": bool(openshell.get("ready")),
    }


def evaluate_cloud_write_permission(
    write_kind: str,
    *,
    operator_approved: bool = False,
    run: Any | None = None,
    probe_openshell: bool = True,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    kind = _text(write_kind).lower()
    if kind in SAFE_LEDGER_WRITE_KINDS:
        return {
            "ok": True,
            "write_kind": kind,
            "reason": "Safe cloud ledger write.",
            "operator_approved": bool(operator_approved),
            "requires_operator_approval": False,
            "requires_openshell": False,
            "openshell_ready": None,
            "actor": "cloud_agent",
            "run_id": int(run.id) if getattr(run, "id", None) is not None else None,
            "context": dict(context or {}),
        }

    status = cloud_write_policy_status(probe_openshell=probe_openshell)
    allowed = bool(status.get("allowed", {}).get(kind, False))
    if kind not in DURABLE_WRITE_KINDS:
        allowed = False
    requires_operator = bool(status.get("require_operator_approval", True))
    requires_openshell = bool(status.get("require_openshell", True))
    openshell_ready = bool(status.get("openshell_ready"))

    reason = "Allowed."
    ok = True
    if not bool(status.get("enabled")):
        ok = False
        reason = "Cloud durable writes are disabled."
    elif not allowed:
        ok = False
        setting = WRITE_KIND_SETTINGS.get(kind)
        reason = f"Cloud durable write kind is not allowed{f' by {setting}' if setting else ''}."
    elif requires_operator and not bool(operator_approved):
        ok = False
        reason = "Operator approval is required for durable cloud writes."
    elif requires_openshell and not openshell_ready:
        ok = False
        reason = "OpenShell is required for durable cloud writes but is not ready."

    return {
        "ok": ok,
        "write_kind": kind,
        "reason": reason,
        "operator_approved": bool(operator_approved),
        "requires_operator_approval": requires_operator,
        "requires_openshell": requires_openshell,
        "openshell_ready": openshell_ready,
        "openshell": status.get("openshell"),
        "actor": "cloud_agent_via_openshell" if requires_openshell else "cloud_agent",
        "run_id": int(run.id) if getattr(run, "id", None) is not None else None,
        "task_kind": _text(getattr(run, "task_kind", "")) or None,
        "provider": _text(getattr(run, "provider", "")) or None,
        "requested_model": _text(getattr(run, "model", "")) or None,
        "routed_model": _text(getattr(run, "routed_model", "")) or None,
        "context": dict(context or {}),
    }


def assert_cloud_write_allowed(write_kind: str, **kwargs: Any) -> dict[str, Any]:
    decision = evaluate_cloud_write_permission(write_kind, **kwargs)
    if not bool(decision.get("ok")):
        raise CloudWritePermissionError(str(decision.get("reason") or "Cloud write was denied."))
    return decision
