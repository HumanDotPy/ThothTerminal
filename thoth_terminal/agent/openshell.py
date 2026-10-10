from __future__ import annotations

import platform
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from thoth_terminal.config import settings
from thoth_terminal.runtime_processes import hidden_subprocess_kwargs


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


@dataclass(frozen=True)
class OpenShellCli:
    mode: str
    argv: tuple[str, ...]
    display: str


def _clean_output(value: str | None) -> str:
    text = _ANSI_RE.sub("", value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()


def _run_command(argv: Sequence[str], *, timeout_s: float) -> dict[str, Any]:
    try:
        # WSL descendants can inherit stdout/stderr pipes. On Windows,
        # subprocess.run then waits for their readers after its timeout, so the
        # configured deadline never reaches the caller. File capture needs no
        # reader threads and remains bounded even when a descendant survives.
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            proc = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                **hidden_subprocess_kwargs(),
            )
            timed_out = False
            try:
                proc.wait(timeout=max(0.01, float(timeout_s)))
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
                proc.wait()
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = _clean_output(stdout_file.read().decode("utf-8", errors="replace"))
            stderr = _clean_output(stderr_file.read().decode("utf-8", errors="replace"))
            if timed_out:
                return {
                    "ok": False,
                    "returncode": None,
                    "stdout": stdout,
                    "stderr": stderr,
                    "error": f"TimeoutExpired: command exceeded {float(timeout_s):.1f}s",
                }
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "ok": proc.returncode == 0,
        "returncode": int(proc.returncode),
        "stdout": stdout,
        "stderr": stderr,
        "error": None if proc.returncode == 0 else (stderr or stdout or f"exit {proc.returncode}"),
    }


def _platform_name() -> str:
    return platform.system().strip().lower()


def _wsl_has_openshell(wsl_exe: str, *, timeout_s: float) -> bool:
    result = _run_command(
        [wsl_exe, "--cd", "~", "sh", "-lc", "command -v openshell >/dev/null 2>&1"],
        timeout_s=float(timeout_s or 3.0),
    )
    if str(result.get("error") or "").startswith("TimeoutExpired:"):
        raise TimeoutError(str(result["error"]))
    return bool(result.get("ok"))


def _wsl_exe_path() -> str | None:
    return shutil.which("wsl.exe") or shutil.which("wsl")


def _run_wsl_user_systemctl(action: str, *, timeout_s: float | None = None) -> dict[str, Any]:
    action_text = str(action or "").strip()
    if action_text not in {"start", "stop", "restart"}:
        raise ValueError("OpenShell gateway action must be start|stop|restart.")
    wsl_exe = _wsl_exe_path()
    if not wsl_exe:
        return {
            "ok": False,
            "action": action_text,
            "returncode": None,
            "stdout": "",
            "stderr": "wsl.exe was not found.",
            "error": "wsl.exe was not found.",
        }
    shell = (
        "export XDG_RUNTIME_DIR=/run/user/$(id -u); "
        "export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus; "
        f"systemctl --user {shlex.quote(action_text)} openshell-gateway"
    )
    result = _run_command(
        [wsl_exe, "--cd", "~", "sh", "-lc", shell],
        timeout_s=max(float(timeout_s if timeout_s is not None else getattr(settings, "openshell_status_timeout_s", 3.0) or 3.0), 30.0),
    )
    result["action"] = action_text
    return result


def start_openshell_gateway(*, verify: bool = True, timeout_s: float | None = None) -> dict[str, Any]:
    """Start the WSL OpenShell gateway user service for Thoth cloud-agent execution."""

    if not bool(getattr(settings, "openshell_enabled", True)):
        return {"ok": True, "started": False, "ready": False, "status": "disabled", "detail": "OpenShell is disabled."}
    result = _run_wsl_user_systemctl("start", timeout_s=timeout_s)
    if not result.get("ok"):
        return {
            "ok": False,
            "started": False,
            "ready": False,
            "status": "start_failed",
            "detail": str(result.get("error") or result.get("stderr") or result.get("stdout") or "OpenShell gateway start failed."),
            "command_result": result,
        }
    status = openshell_runtime_status(probe_gateway=True) if verify else {"ready": True, "status": "start_requested"}
    return {
        "ok": bool(status.get("ready")) if verify else True,
        "started": True,
        "ready": bool(status.get("ready")),
        "status": status.get("status") or "started",
        "detail": status.get("detail") or "OpenShell gateway start requested.",
        "runtime_status": status,
    }


def stop_openshell_gateway(*, timeout_s: float | None = None) -> dict[str, Any]:
    """Stop the WSL OpenShell gateway user service when Thoth exits."""

    if not bool(getattr(settings, "openshell_enabled", True)):
        return {"ok": True, "stopped": False, "status": "disabled", "detail": "OpenShell is disabled."}
    result = _run_wsl_user_systemctl("stop", timeout_s=timeout_s)
    return {
        "ok": bool(result.get("ok")),
        "stopped": bool(result.get("ok")),
        "status": "stopped" if result.get("ok") else "stop_failed",
        "detail": str(result.get("error") or result.get("stderr") or result.get("stdout") or ""),
        "command_result": result,
    }


def resolve_openshell_cli(*, prefer_wsl: bool | None = None, timeout_s: float | None = None) -> OpenShellCli | None:
    """Resolve the OpenShell CLI without assuming the gateway is configured."""

    timeout = float(timeout_s if timeout_s is not None else getattr(settings, "openshell_status_timeout_s", 3.0) or 3.0)
    prefer_wsl_value = (
        bool(getattr(settings, "openshell_prefer_wsl", True))
        if prefer_wsl is None
        else bool(prefer_wsl)
    )
    native = shutil.which("openshell")
    is_windows = _platform_name() == "windows"
    if native and (not is_windows or not prefer_wsl_value):
        return OpenShellCli(mode="native", argv=(native,), display=native)

    if is_windows:
        wsl_exe = _wsl_exe_path()
        if wsl_exe and _wsl_has_openshell(wsl_exe, timeout_s=timeout):
            return OpenShellCli(
                mode="wsl",
                argv=(wsl_exe, "--cd", "~", "openshell"),
                display="wsl.exe --cd ~ openshell",
            )

    if native:
        return OpenShellCli(mode="native", argv=(native,), display=native)
    return None


def _run_openshell(cli: OpenShellCli, args: Sequence[str], *, timeout_s: float) -> dict[str, Any]:
    command_timeout = max(float(timeout_s or 3.0), 30.0) if cli.mode == "wsl" else timeout_s
    return _run_command([*cli.argv, *[str(arg) for arg in args]], timeout_s=command_timeout)


def _parse_gateway_status(text: str, *, returncode: int | None) -> dict[str, Any]:
    lower = text.lower()
    if not text:
        return {
            "status": "status_unavailable",
            "gateway_configured": False,
            "gateway_connected": False,
            "detail": "OpenShell status returned no output.",
        }
    if "no gateway configured" in lower:
        return {
            "status": "no_gateway_configured",
            "gateway_configured": False,
            "gateway_connected": False,
            "detail": "No gateway configured.",
        }
    if returncode not in {0, None}:
        return {
            "status": "status_error",
            "gateway_configured": "gateway" in lower,
            "gateway_connected": False,
            "detail": text.splitlines()[-1][:240] if text.splitlines() else "OpenShell status failed.",
        }
    if "connected" in lower and "not connected" not in lower:
        return {
            "status": "connected",
            "gateway_configured": True,
            "gateway_connected": True,
            "detail": "Gateway connected.",
        }
    if "gateway" in lower:
        return {
            "status": "gateway_unknown",
            "gateway_configured": True,
            "gateway_connected": False,
            "detail": text.splitlines()[-1][:240] if text.splitlines() else "Gateway state unknown.",
        }
    return {
        "status": "unknown",
        "gateway_configured": False,
        "gateway_connected": False,
        "detail": text.splitlines()[-1][:240] if text.splitlines() else "OpenShell state unknown.",
    }


def openshell_runtime_status(*, probe_gateway: bool = True) -> dict[str, Any]:
    timeout = max(0.01, float(getattr(settings, "openshell_status_timeout_s", 3.0) or 3.0))
    deadline = time.monotonic() + timeout
    require_gateway = bool(getattr(settings, "openshell_require_gateway", True))
    enabled = bool(getattr(settings, "openshell_enabled", True))
    discovery_error = None
    try:
        cli = resolve_openshell_cli(timeout_s=timeout)
    except TimeoutError as exc:
        cli = None
        discovery_error = str(exc)
    payload: dict[str, Any] = {
        "enabled": enabled,
        "installed": cli is not None,
        "ready": False,
        "mode": cli.mode if cli else None,
        "command": cli.display if cli else None,
        "version": None,
        "status": "missing_cli" if cli is None else "installed",
        "gateway_configured": False,
        "gateway_connected": False,
        "require_gateway": require_gateway,
        "detail": "",
        "install_hint": "",
    }
    if discovery_error:
        payload.update(installed=None, ready=None, status="probe_timeout", detail=discovery_error)
        return payload
    if cli is None:
        payload["detail"] = "OpenShell CLI was not found on PATH or in the default WSL distribution."
        payload["install_hint"] = (
            "Install OpenShell natively or in WSL, then make `openshell` reachable from the Thoth runtime."
        )
        return payload

    version = _run_openshell(cli, ["--version"], timeout_s=max(0.01, deadline - time.monotonic()))
    if version.get("ok"):
        payload["version"] = (version.get("stdout") or version.get("stderr") or "").strip() or None
    else:
        payload["status"] = "probe_timeout" if str(version.get("error") or "").startswith("TimeoutExpired:") else "version_error"
        if payload["status"] == "probe_timeout":
            payload["ready"] = None
        payload["detail"] = str(version.get("error") or "OpenShell version check failed.")
        return payload

    if not probe_gateway:
        payload["ready"] = enabled and not require_gateway
        payload["detail"] = "OpenShell CLI is installed; gateway probe skipped."
        return payload

    status_result = _run_openshell(cli, ["status"], timeout_s=max(0.01, deadline - time.monotonic()))
    if str(status_result.get("error") or "").startswith("TimeoutExpired:"):
        payload.update(ready=None, status="probe_timeout", detail=str(status_result["error"]))
        return payload
    raw_status = "\n".join(
        part for part in [str(status_result.get("stdout") or ""), str(status_result.get("stderr") or "")] if part
    ).strip()
    parsed = _parse_gateway_status(raw_status, returncode=status_result.get("returncode"))
    payload.update(parsed)
    payload["raw_status"] = raw_status[:4000]
    payload["ready"] = enabled and bool(payload["installed"]) and (
        bool(payload["gateway_connected"]) or not require_gateway
    )
    if payload["status"] == "no_gateway_configured":
        payload["install_hint"] = (
            "Start or register an OpenShell gateway before assigning cloud agents to the sandbox runner."
        )
    return payload


def build_sandbox_create_args(
    command: Sequence[str],
    *,
    name: str | None = None,
    from_image: str | None = None,
    policy_path: str | Path | None = None,
    providers: Sequence[str] | None = None,
    labels: dict[str, str] | None = None,
    no_keep: bool = True,
    auto_providers: bool = False,
) -> list[str]:
    """Build a noninteractive `openshell sandbox create` command payload."""

    cmd = [str(item) for item in command if str(item)]
    if not cmd:
        raise ValueError("OpenShell sandbox command cannot be empty.")
    args: list[str] = ["sandbox", "create"]
    if name:
        args.extend(["--name", str(name)])
    if from_image:
        args.extend(["--from", str(from_image)])
    if no_keep:
        args.append("--no-keep")
    if policy_path:
        args.extend(["--policy", str(policy_path)])
    for provider in providers or []:
        provider_value = str(provider or "").strip()
        if provider_value:
            args.extend(["--provider", provider_value])
    args.append("--auto-providers" if auto_providers else "--no-auto-providers")
    for key, value in (labels or {}).items():
        key_text = str(key or "").strip()
        value_text = str(value or "").strip()
        if key_text and value_text:
            args.extend(["--label", f"{key_text}={value_text}"])
    args.append("--")
    args.extend(cmd)
    return args


def sandbox_create_preview(command: Sequence[str], **kwargs: Any) -> dict[str, Any]:
    args = build_sandbox_create_args(command, **kwargs)
    cli = resolve_openshell_cli()
    return {
        "available": cli is not None,
        "mode": cli.mode if cli else None,
        "command": f"{cli.display} {shlex.join(args)}" if cli else f"openshell {shlex.join(args)}",
        "args": args,
    }
