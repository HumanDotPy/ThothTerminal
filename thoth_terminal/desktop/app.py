from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QIcon
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QMenu, QMessageBox, QProgressBar, QSystemTrayIcon, QVBoxLayout, QWidget

from thoth_terminal.config import settings
from thoth_terminal.crash_reports import (
    begin_desktop_session,
    crash_reports_dir,
    crash_status_payload,
    create_support_bundle,
    install_crash_hooks,
    mark_desktop_session_clean,
    update_desktop_session,
    write_crash_report,
)
from thoth_terminal.db import database_backend
from thoth_terminal.desktop.autostart import (
    AutostartError,
    desktop_command,
    is_enabled as launch_at_login_enabled,
    is_supported as launch_at_login_supported,
    set_enabled as set_launch_at_login_enabled,
)
from thoth_terminal.desktop.control_api import DesktopControlServer
from thoth_terminal.desktop.instance import (
    acquire_single_instance_mutex,
    clear_instance_lock,
    pid_alive,
    read_existing_instance,
    terminate_instance,
    terminate_desktop_processes,
    terminate_thoth_support_processes,
    write_instance_lock,
)
from thoth_terminal.desktop.prefs import DesktopPreferenceStore, DesktopPreferences
from thoth_terminal.desktop.runtime import (
    configure_desktop_runtime,
    desktop_logs_dir,
    open_path_in_file_manager,
    write_desktop_runtime_state,
)
from thoth_terminal.desktop.support_supervisor import SupportSupervisor
from thoth_terminal.desktop.tunnel import DesktopTunnelProcess, start_desktop_tunnel
from thoth_terminal.desktop.updater import apply_local_update, check_local_update, repair_desktop_integration
from thoth_terminal.desktop.windows_identity import set_windows_process_identity
from thoth_terminal.integrations.ibkr.gateway import gateway_browser_login_url, launch_gateway_if_needed, stop_managed_gateway
from thoth_terminal.migrations import init_db
from thoth_terminal.runtime_processes import run_hidden_process, start_hidden_logged_process
from thoth_terminal.runtime_env import repo_root
from thoth_terminal.runtime_info import read_gaming_mode_state
from thoth_terminal.runtime_supervisor import (
    ManagedRuntimeGroup,
    ManagedRuntimeProcess,
    runtime_child_command,
    runtime_child_environment,
)
from thoth_terminal.security import validate_security_posture


logger = logging.getLogger("thoth.desktop")
WINDOW_TITLE = "Thoth Terminal"
_FILE_LOGGING_CONFIGURED = False
LOCAL_POSTGRES_CONTAINER = "thoth-postgres"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _storage_recovery_readiness() -> dict[str, object]:
    root = repo_root()
    broker = Path(os.environ.get("ProgramData", "C:/ProgramData")) / "ThothTerminal" / "storage-recovery" / "ThothStorageRecoveryBroker.ps1"
    if os.name != "nt" or not broker.is_file():
        return {"applicable": False, "ok": True}
    script = root / "scripts" / "Status-ThothStorageRecoveryBroker.ps1"
    if not script.is_file():
        return {"applicable": False, "ok": True}
    try:
        result = run_hidden_process(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
        payload = json.loads(result.stdout)
        if isinstance(payload, dict):
            return {
                "applicable": True,
                "ok": result.returncode == 0 and payload.get("ok") is True,
                "reason": str(payload.get("reason") or ""),
            }
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return {"applicable": True, "ok": False, "reason": "status_unavailable"}


def _shutdown_playwright_connectors() -> None:
    try:
        from thoth_terminal.connectors import shutdown_playwright_pool

        shutdown_playwright_pool(wait=False, cancel_futures=True)
    except Exception:
        logger.warning("desktop shutdown failed to stop Playwright connector pool", exc_info=True)


def _force_process_exit_later(delay_s: float = 45.0, exit_code: int = 0) -> None:
    def _worker() -> None:
        time.sleep(max(5.0, float(delay_s)))
        logger.error("desktop process still alive after quit grace period; forcing process exit")
        os._exit(int(exit_code))

    threading.Thread(target=_worker, name="thoth-desktop-force-exit", daemon=True).start()


def _configure_desktop_file_logging() -> None:
    global _FILE_LOGGING_CONFIGURED
    if _FILE_LOGGING_CONFIGURED:
        return
    log_path = desktop_logs_dir() / "desktop.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Keep a crash trace without allowing a permanently running desktop to grow
    # one unbounded log on the same disk that hosts the database/object store.
    handler = RotatingFileHandler(
        log_path,
        maxBytes=20 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(getattr(logging, str(settings.log_level or "INFO").upper(), logging.INFO))
    _FILE_LOGGING_CONFIGURED = True
    logger.info("desktop file logging enabled: %s", log_path)


def _maybe_start_desktop_tunnel(base_url: str) -> DesktopTunnelProcess | None:
    try:
        tunnel = start_desktop_tunnel(base_url)
    except Exception:
        logger.exception("desktop failed to start cloudflared tunnel")
        return None
    if tunnel is not None:
        logger.info("desktop started cloudflared tunnel pid=%s config=%s", tunnel.pid, tunnel.config_path)
    return tunnel


def _app_icon() -> QIcon:
    filename = "thoth-terminal.ico" if os.name == "nt" else "thoth-terminal-1024.png"
    icon_path = Path(__file__).resolve().parent.parent / "web" / "static" / "icons" / filename
    return QIcon(str(icon_path))


def _maybe_show_launch_failure_dialog(exc: BaseException, *, start_hidden: bool) -> None:
    if start_hidden:
        logger.error("desktop hidden launch failed; suppressing foreground error dialog: %s", exc)
        return
    try:
        if QApplication.instance() is not None:
            QMessageBox.critical(None, WINDOW_TITLE, f"Desktop launch failed.\n\n{exc}")
    except Exception:
        logger.warning("desktop failed to show launch failure dialog", exc_info=True)


class StartupWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{WINDOW_TITLE} Loading")
        self.setWindowIcon(_app_icon())
        self.setFixedSize(460, 240)
        self.setWindowFlags(Qt.Window | Qt.CustomizeWindowHint | Qt.WindowTitleHint)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        title = QLabel("THOTH TERMINAL")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 24px; font-weight: 800; letter-spacing: 2px;")
        self.message = QLabel("Starting local research terminal...")
        self.message.setAlignment(Qt.AlignCenter)
        self.message.setWordWrap(True)
        self.message.setStyleSheet("font-size: 12px; color: #b9a7ff;")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)

        layout.addStretch(1)
        layout.addWidget(title)
        layout.addWidget(self.message)
        layout.addWidget(self.progress)
        layout.addStretch(1)

        self.setStyleSheet(
            """
            QWidget { background: #050308; color: #f2efff; font-family: Consolas, monospace; }
            QProgressBar { border: 1px solid #7c4dff; background: #120b1f; height: 8px; }
            QProgressBar::chunk { background: #8b5cf6; }
            """
        )

    def set_message(self, text: str) -> None:
        self.message.setText(text)
        app = QApplication.instance()
        if app is not None:
            app.processEvents()


def _pick_available_port(host: str, preferred_port: int) -> int:
    candidate = int(preferred_port)
    for port in (candidate, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, port))
                return int(sock.getsockname()[1])
            except OSError:
                continue
    raise RuntimeError(f"Unable to allocate a listening port on {host}.")


def _postgres_endpoint_from_url(raw_url: str | None) -> tuple[str, int] | None:
    value = str(raw_url or "").strip()
    if not value:
        return None
    if not value.lower().startswith(("postgresql://", "postgresql+psycopg://", "postgres://")):
        return None
    parsed = urlparse(value.replace("postgresql+psycopg://", "postgresql://", 1))
    if not parsed.hostname:
        return None
    return parsed.hostname, int(parsed.port or 5432)


def _tcp_endpoint_ready(host: str, port: int, *, timeout_s: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout_s):
            return True
    except OSError:
        return False


def _stop_local_postgres_container(container_name: str = LOCAL_POSTGRES_CONTAINER) -> bool:
    try:
        running = run_hidden_process(
            ["docker", "ps", "--filter", f"name=^/{container_name}$", "--format", "{{.Names}}"],
            timeout_s=8,
        )
    except Exception:
        logger.warning("desktop could not inspect local Postgres container for shutdown", exc_info=True)
        return False
    if running.returncode != 0:
        logger.warning("desktop local Postgres inspect failed: %s", (running.stderr or running.stdout or "").strip())
        return False
    names = {line.strip() for line in (running.stdout or "").splitlines() if line.strip()}
    if container_name not in names:
        return False
    try:
        stopped = run_hidden_process(["docker", "stop", container_name], timeout_s=45)
    except Exception:
        logger.warning("desktop local Postgres stop failed", exc_info=True)
        return False
    if stopped.returncode != 0:
        logger.warning("desktop local Postgres stop failed: %s", (stopped.stderr or stopped.stdout or "").strip())
        return False
    logger.info("desktop stopped local Postgres container %s", container_name)
    return True


def _uses_default_local_postgres() -> bool:
    if database_backend() != "postgresql":
        return False
    endpoint = _postgres_endpoint_from_url(str(settings.database_url or ""))
    if endpoint is None:
        return False
    host, port = endpoint
    return host.strip().lower() in {"127.0.0.1", "localhost", "::1"} and int(port) == 55432


def _stop_desktop_local_postgres() -> bool:
    if not _uses_default_local_postgres():
        return False
    return _stop_local_postgres_container()


def _ensure_desktop_postgres_ready() -> bool:
    if database_backend() != "postgresql":
        return False
    endpoint = _postgres_endpoint_from_url(str(settings.database_url or ""))
    if endpoint is None:
        return False
    host, port = endpoint
    if _tcp_endpoint_ready(host, port):
        return False
    local_hosts = {"127.0.0.1", "localhost", "::1"}
    if host.strip().lower() in local_hosts and int(port) == 55432:
        script = repo_root() / "scripts" / "start-local-postgres.ps1"
        if script.exists():
            logger.warning("desktop Postgres %s:%s is unavailable; attempting local Docker Postgres start", host, port)
            try:
                run_hidden_process(
                    ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                    cwd=str(repo_root()),
                    timeout_s=660,
                    check=True,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    f"Configured Postgres {host}:{port} is not reachable, and Docker local Postgres did not start within 660s. "
                    "Docker Desktop may be hung; restart Docker Desktop or unset THOTH_DATABASE_URL to use SQLite."
                ) from exc
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or exc.stdout or "").strip()
                raise RuntimeError(
                    f"Configured Postgres {host}:{port} is not reachable, and local Postgres autostart failed. {detail}"
                ) from exc
            if _tcp_endpoint_ready(host, port, timeout_s=3.0):
                return True
    raise RuntimeError(
        f"Configured Postgres {host}:{port} is not reachable. Start Docker Desktop/local Postgres first, "
        "or unset THOTH_DATABASE_URL to use SQLite."
    )


def _wait_for_server(
    base_url: str,
    *,
    timeout_s: float = 120.0,
    server_thread: object | None = None,
) -> None:
    deadline = time.time() + timeout_s
    url = f"{base_url.rstrip('/')}/ops/live"
    last_error: str | None = None
    while time.time() < deadline:
        if server_thread is not None and server_thread.error is not None:
            raise RuntimeError(f"Desktop web server failed before health check: {server_thread.error}") from server_thread.error
        if server_thread is not None and (not server_thread.is_alive()):
            raise RuntimeError("Desktop web server stopped before health check completed.")
        try:
            response = requests.get(url, timeout=1.5)
            if response.ok:
                return
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.25)
    detail = f" Last probe error: {last_error}" if last_error else ""
    raise RuntimeError(f"Timed out waiting for desktop web server at {url} after {timeout_s:.0f}s.{detail}")


def _wait_for_local_agent_api(
    base_url: str,
    *,
    timeout_s: float = 45.0,
    server_thread: object | None = None,
    required: bool = True,
) -> bool:
    deadline = time.time() + timeout_s
    url = f"{base_url.rstrip('/')}/api/local-agent/health"
    last_error: str | None = None
    while time.time() < deadline:
        if server_thread is not None and server_thread.error is not None:
            if required:
                raise RuntimeError(f"Local agent API failed before health check: {server_thread.error}") from server_thread.error
            logger.warning("local agent API failed before health check; continuing desktop startup", exc_info=server_thread.error)
            return False
        if server_thread is not None and (not server_thread.is_alive()):
            if required:
                raise RuntimeError("Local agent API stopped before health check completed.")
            logger.warning("local agent API stopped before health check completed; continuing desktop startup")
            return False
        payload, error = _local_agent_api_health(base_url, timeout_s=1.5)
        if payload is not None:
            if _local_agent_api_payload_ready(payload):
                return True
            if not required and _local_agent_api_payload_serving(payload):
                logger.warning(
                    "local agent API is serving authenticated health but runtime metadata is not fully ready; "
                    "continuing desktop startup. payload=%s",
                    payload,
                )
                return True
            last_error = f"health returned enabled={payload.get('enabled')} token_configured={payload.get('token_configured')}"
        elif error:
            last_error = error
        time.sleep(0.25)
    detail = f" Last probe error: {last_error}" if last_error else ""
    if required:
        raise RuntimeError(f"Timed out waiting for local agent API at {url} after {timeout_s:.0f}s.{detail}")
    logger.warning("timed out waiting for local agent API at %s after %.0fs.%s Continuing desktop startup.", url, timeout_s, detail)
    return False


def _local_agent_api_health(base_url: str, *, timeout_s: float = 1.5) -> tuple[dict[str, object] | None, str | None]:
    url = f"{base_url.rstrip('/')}/api/local-agent/health"
    try:
        response = requests.get(url, timeout=max(0.1, float(timeout_s)))
        if not response.ok:
            return None, f"health returned HTTP {response.status_code}"
        payload = response.json()
        if not isinstance(payload, dict):
            return None, "health returned non-object JSON"
        return dict(payload), None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _local_agent_api_ready(base_url: str, *, timeout_s: float = 1.5) -> bool:
    payload, _error = _local_agent_api_health(base_url, timeout_s=timeout_s)
    return _local_agent_api_payload_ready(payload)


def _local_agent_api_payload_ready(payload: dict[str, object] | None) -> bool:
    if not (payload and payload.get("enabled") and payload.get("token_configured")):
        return False
    runtime = payload.get("runtime")
    if not isinstance(runtime, dict):
        return False
    current_git_sha = str(runtime.get("current_git_sha") or "").strip()
    drift = runtime.get("deployment_drift")
    if not current_git_sha or not isinstance(drift, dict):
        return False
    return not bool(drift.get("stale_checkout"))


def _local_agent_api_payload_serving(payload: dict[str, object] | None) -> bool:
    return bool(payload and payload.get("ok", True) and payload.get("enabled") and payload.get("token_configured"))


def _tcp_bind_available(host: str, port: int) -> tuple[bool, str | None]:
    bind_host = str(host or "127.0.0.1").strip() or "127.0.0.1"
    family = socket.AF_INET6 if ":" in bind_host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.bind((bind_host, int(port)))
        return True, None
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _wait_for_tcp_bind_available(host: str, port: int, *, timeout_s: float = 6.0) -> bool:
    deadline = time.time() + max(0.1, float(timeout_s))
    while time.time() < deadline:
        available, _error = _tcp_bind_available(host, port)
        if available:
            return True
        time.sleep(0.25)
    available, _error = _tcp_bind_available(host, port)
    return available


def _prepare_local_agent_api_start(base_url: str, *, bind_host: str, bind_port: int) -> bool:
    """Return True when an existing API should be reused, False when desktop should start one."""
    payload, probe_error = _local_agent_api_health(base_url, timeout_s=1.5)
    if _local_agent_api_payload_ready(payload):
        return True

    bind_available, bind_error = _tcp_bind_available(bind_host, bind_port)
    if bind_available:
        return False

    payload, probe_error = _local_agent_api_health(base_url, timeout_s=5.0)
    if _local_agent_api_payload_ready(payload):
        return True

    logger.warning(
        "local agent API port %s is occupied but not reusable; attempting stale support cleanup. bind_error=%s probe_error=%s payload=%s",
        bind_port,
        bind_error,
        probe_error,
        payload,
    )
    terminate_thoth_support_processes()
    if _wait_for_tcp_bind_available(bind_host, bind_port, timeout_s=6.0):
        return False

    payload, probe_error = _local_agent_api_health(base_url, timeout_s=2.0)
    if _local_agent_api_payload_ready(payload):
        return True
    if payload is not None:
        detail = f"health returned enabled={payload.get('enabled')} token_configured={payload.get('token_configured')}"
    else:
        detail = probe_error or bind_error or "port remained unavailable"
    raise RuntimeError(
        "Local agent API port "
        f"{bind_host}:{bind_port} is already in use, but the service on that port is not a reusable Thoth local-agent API. "
        f"{detail}. Stop the process using port {bind_port}, or set THOTH_AGENT_API_PORT to a free port before launching Thoth Terminal."
    )


def _agent_api_token_path() -> Path:
    return Path(settings.runtime_state_dir) / "local-agent-api.token.txt"


def _legacy_agent_api_token_path() -> Path:
    return repo_root() / "tmp-local-agent-api.token.txt"


def _resolve_agent_api_token() -> str:
    configured = str(getattr(settings, "agent_api_token", "") or "").strip()
    if configured:
        return configured
    for path in (_agent_api_token_path(), _legacy_agent_api_token_path()):
        try:
            token = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue
        except Exception:
            logger.warning("Unable to read local agent API token from %s", path, exc_info=True)
            continue
        if token:
            settings.agent_api_token = token
            _persist_agent_api_token(token)
            return token
    token = secrets.token_urlsafe(32)
    settings.agent_api_token = token
    _persist_agent_api_token(token)
    return token


def _persist_agent_api_token(token: str) -> None:
    path = _agent_api_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(token.strip() + "\n", encoding="utf-8")
    except Exception:
        logger.warning("Unable to persist local agent API token to %s", path, exc_info=True)


class ManagedWorkerProcess(ManagedRuntimeGroup):
    def __init__(self) -> None:
        root = repo_root()
        env = runtime_child_environment()
        common = {
            "cwd": root,
            "env": env,
            "poll_interval_s": float(getattr(settings, "runtime_child_memory_poll_s", 0.25) or 0.25),
            "restart_max_delay_s": float(getattr(settings, "runtime_child_restart_max_delay_s", 30.0) or 30.0),
            "shutdown_timeout_s": float(getattr(settings, "runtime_child_shutdown_timeout_s", 8.0) or 8.0),
        }
        super().__init__(
            ManagedRuntimeProcess(
                name="worker-main",
                command=runtime_child_command("worker-main"),
                memory_limit_mb=int(getattr(settings, "worker_process_memory_limit_mb", 3072) or 3072),
                heartbeat_service="worker",
                heartbeat_max_age_s=90.0,
                heartbeat_startup_grace_s=120.0,
                **common,
            ),
            ManagedRuntimeProcess(
                name="worker-cloud",
                command=runtime_child_command("worker-cloud"),
                memory_limit_mb=int(getattr(settings, "cloud_worker_process_memory_limit_mb", 3072) or 3072),
                heartbeat_service="cloud-worker",
                heartbeat_max_age_s=90.0,
                heartbeat_startup_grace_s=120.0,
                **common,
            ),
            name="desktop-workers",
        )


class ManagedLocalAgentApiProcess(ManagedRuntimeProcess):
    def __init__(self, *, host: str, port: int) -> None:
        super().__init__(
            name="local-agent-api",
            command=runtime_child_command("local-agent-api", host=host, port=port),
            cwd=repo_root(),
            env=runtime_child_environment(overrides={"THOTH_AGENT_API_ENABLED": "true"}),
            memory_limit_mb=int(getattr(settings, "agent_api_process_memory_limit_mb", 1024) or 1024),
            poll_interval_s=float(getattr(settings, "runtime_child_memory_poll_s", 0.25) or 0.25),
            restart_max_delay_s=float(getattr(settings, "runtime_child_restart_max_delay_s", 30.0) or 30.0),
            shutdown_timeout_s=float(getattr(settings, "runtime_child_shutdown_timeout_s", 8.0) or 8.0),
        )


class ManagedWebProcess(ManagedRuntimeProcess):
    def __init__(self, *, host: str, port: int, control_url: str, control_token: str) -> None:
        super().__init__(
            name="web",
            command=runtime_child_command("web", host=host, port=port),
            cwd=repo_root(),
            env=runtime_child_environment(
                overrides={
                    "THOTH_DESKTOP_CONTROL_URL": str(control_url),
                    "THOTH_DESKTOP_CONTROL_TOKEN": str(control_token),
                }
            ),
            memory_limit_mb=int(getattr(settings, "web_process_memory_limit_mb", 2048) or 2048),
            poll_interval_s=float(getattr(settings, "runtime_child_memory_poll_s", 0.25) or 0.25),
            restart_max_delay_s=float(getattr(settings, "runtime_child_restart_max_delay_s", 30.0) or 30.0),
            shutdown_timeout_s=float(getattr(settings, "runtime_child_shutdown_timeout_s", 8.0) or 8.0),
        )


def _stop_thread(thread: object | None, *, name: str, timeout_s: float = 5.0) -> None:
    if thread is None:
        return
    try:
        stop = getattr(thread, "stop", None)
        if callable(stop):
            stop()
    except Exception:
        logger.warning("desktop startup cleanup failed to signal %s stop", name, exc_info=True)
    try:
        join = getattr(thread, "join", None)
        if callable(join):
            join(timeout=max(0.1, float(timeout_s)))
    except Exception:
        logger.warning("desktop startup cleanup failed to join %s", name, exc_info=True)
    try:
        is_alive = getattr(thread, "is_alive", None)
        if callable(is_alive) and is_alive():
            logger.warning("desktop startup cleanup left %s alive after %.1fs", name, timeout_s)
    except Exception:
        pass


def _stop_spawned_process_tree(pid: object | None, *, label: str) -> None:
    try:
        root_pid = int(pid or 0)
    except Exception:
        root_pid = 0
    if root_pid <= 0:
        return
    if not pid_alive(root_pid):
        return
    logger.warning("desktop startup cleanup stopping spawned %s process tree pid=%s", label, root_pid)
    if os.name == "nt":
        script = f"""
$rootPid = {root_pid}
function Stop-Tree([int]$ProcessId) {{
  Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object {{ $_.ParentProcessId -eq $ProcessId }} |
    ForEach-Object {{ Stop-Tree ([int]$_.ProcessId) }}
  Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
}}
Stop-Tree $rootPid
"""
        try:
            run_hidden_process(
                ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-Command", script],
                check=False,
                timeout_s=12.0,
            )
        except Exception:
            logger.warning("desktop startup cleanup failed to stop %s pid=%s", label, root_pid, exc_info=True)
        return
    try:
        os.kill(root_pid, 15)
    except Exception:
        logger.warning("desktop startup cleanup failed to stop %s pid=%s", label, root_pid, exc_info=True)


def _remove_runtime_state_file(service: str) -> None:
    try:
        state_path = Path(settings.runtime_state_dir) / f"{service}.json"
        state_path.unlink(missing_ok=True)
    except Exception:
        logger.warning("desktop startup cleanup failed to remove %s runtime state", service, exc_info=True)


def _payload_status(payload: dict[str, object] | None) -> str:
    return str((payload or {}).get("status") or "").strip().lower()


def _payload_was_launched(payload: dict[str, object] | None) -> bool:
    if not payload:
        return False
    status = _payload_status(payload)
    return bool(payload.get("launched")) or status.startswith("launched")


def _cleanup_failed_desktop_launch(
    *,
    startup: object | None = None,
    window: object | None = None,
    tray: object | None = None,
    web_app: object | None = None,
    server_thread: ManagedWebProcess | None = None,
    agent_api_thread: ManagedLocalAgentApiProcess | None = None,
    worker_thread: ManagedWorkerProcess | None = None,
    control_server: DesktopControlServer | None = None,
    local_model_payload: dict[str, object] | None = None,
    gateway_payload: dict[str, object] | None = None,
    tunnel_process: DesktopTunnelProcess | None = None,
    local_postgres_started_by_desktop: bool = False,
) -> None:
    logger.warning("desktop launch failed; cleaning up startup resources")
    for label, obj in (("startup window", startup), ("main window", window)):
        if obj is None:
            continue
        try:
            close = getattr(obj, "close", None)
            if callable(close):
                close()
        except Exception:
            logger.warning("desktop startup cleanup failed to close %s", label, exc_info=True)
    if tray is not None:
        try:
            hide = getattr(tray, "hide", None)
            if callable(hide):
                hide()
        except Exception:
            logger.warning("desktop startup cleanup failed to hide tray", exc_info=True)

    _stop_thread(worker_thread, name="worker thread")
    if control_server is not None:
        try:
            control_server.stop()
        except Exception:
            logger.warning("desktop startup cleanup failed to stop control server", exc_info=True)
    _stop_thread(agent_api_thread, name="local agent API thread")
    _stop_thread(server_thread, name="web server thread")

    if _payload_was_launched(local_model_payload):
        _stop_spawned_process_tree((local_model_payload or {}).get("pid"), label="local-model")
        _remove_runtime_state_file("local-model")

    if _payload_was_launched(gateway_payload):
        try:
            payload = stop_managed_gateway()
            if not payload.get("ok"):
                logger.warning("desktop startup cleanup IBKR gateway shutdown failed: %s", payload)
        except Exception:
            logger.warning("desktop startup cleanup failed to stop IBKR gateway", exc_info=True)

    if tunnel_process is not None:
        try:
            tunnel_process.stop()
            _remove_runtime_state_file("cloudflared")
        except Exception:
            logger.warning("desktop startup cleanup failed to stop cloudflared tunnel", exc_info=True)

    if server_thread is not None or agent_api_thread is not None or worker_thread is not None or tunnel_process is not None:
        try:
            terminate_thoth_support_processes()
        except Exception:
            logger.warning("desktop startup cleanup failed to terminate support processes", exc_info=True)

    _stop_desktop_local_postgres()

    if web_app is not None:
        try:
            web_app.state.desktop_bridge = None
        except Exception:
            logger.warning("desktop startup cleanup failed to clear web desktop bridge", exc_info=True)
    try:
        clear_instance_lock()
    except Exception:
        logger.warning("desktop startup cleanup failed to clear instance lock", exc_info=True)


class DesktopController(QObject):
    show_window_requested = Signal()
    hide_window_requested = Signal()
    quit_requested = Signal()
    ibkr_action_requested = Signal(str)

    def __init__(
        self,
        *,
        pref_store: DesktopPreferenceStore,
        base_url: str,
        server_thread: ManagedWebProcess,
        agent_api_thread: ManagedLocalAgentApiProcess | None,
        worker_thread: ManagedWorkerProcess | None,
        tunnel_process: DesktopTunnelProcess | None = None,
        local_postgres_started_by_desktop: bool = False,
    ) -> None:
        super().__init__()
        self._pref_store = pref_store
        self._base_url = str(base_url)
        self._server_thread = server_thread
        self._agent_api_thread = agent_api_thread
        self._worker_thread = worker_thread
        self._tunnel_process = tunnel_process
        self._local_postgres_started_by_desktop = bool(local_postgres_started_by_desktop)
        self._window = None
        self._tray = None
        self._window_visible = False
        self._lock = threading.RLock()
        self._tray_hint_shown = False
        self._ibkr_sign_in = None
        self._update_thread: threading.Thread | None = None
        self._update_state: dict[str, object] = {
            "state": "idle",
            "checked_at": None,
            "update_available": False,
            "can_update": False,
        }

        self.show_window_requested.connect(self._show_window)
        self.hide_window_requested.connect(self._hide_window)
        self.quit_requested.connect(self._quit_application)
        self.ibkr_action_requested.connect(self._handle_ibkr_action)

    def attach_window(self, window: "ThothMainWindow", tray: QSystemTrayIcon | None) -> None:
        with self._lock:
            self._window = window
            self._tray = tray

    def attach_tunnel(self, tunnel_process: DesktopTunnelProcess | None) -> None:
        with self._lock:
            self._tunnel_process = tunnel_process
            window = self._window
            if window is not None:
                self._window_visible = bool(window.isVisible())

    def current_preferences(self) -> DesktopPreferences:
        return self._pref_store.load()

    def tray_available(self) -> bool:
        with self._lock:
            return bool(self._tray and self._tray.isVisible())

    def set_window_visible(self, visible: bool) -> None:
        with self._lock:
            self._window_visible = bool(visible)

    def status_payload(self) -> dict[str, object]:
        from thoth_terminal.desktop.ibkr_credentials import IbkrCredentialStore
        from thoth_terminal.security import normalized_mode

        # Probe only native presence. Passwords never enter bridge responses.
        ibkr_supported = os.name == "nt" and normalized_mode(settings.mode) == "local_desktop"
        ibkr_saved = False
        if ibkr_supported and self._ibkr_sign_in is None:
            try:
                ibkr_saved = IbkrCredentialStore(gateway_browser_login_url()).exists()
            except (OSError, ValueError, RuntimeError):
                pass
        ibkr_login = self._ibkr_sign_in
        prefs = self.current_preferences()
        autostart_supported = launch_at_login_supported()
        autostart_enabled = False
        autostart_error = None
        if autostart_supported:
            try:
                autostart_enabled = launch_at_login_enabled()
            except Exception as exc:
                autostart_error = f"{type(exc).__name__}: {exc}"
        with self._lock:
            window_visible = bool(self._window_visible)
        return {
            "enabled": True,
            "base_url": self._base_url,
            "launch_at_login_supported": autostart_supported,
            "launch_at_login_enabled": autostart_enabled,
            "autostart_error": autostart_error,
            "preferences": prefs.to_dict(),
            "ibkr_sign_in": ({"supported": ibkr_supported, "credentials_saved": ibkr_saved,
                               "state": "idle", "message": "Saved sign-in is available from Integrations."}
                              if ibkr_login is None else ibkr_login.status()),
            "paths": {
                "database_backend": database_backend(),
                "db_path": str(settings.db_path),
                "database_url_configured": bool(str(getattr(settings, "database_url", "") or "").strip()),
                "data_dir": str(settings.data_dir),
                "objects_dir": str(settings.objects_dir),
                "runtime_state_dir": str(settings.runtime_state_dir),
                "logs_dir": str(desktop_logs_dir()),
                "crash_reports_dir": str(crash_reports_dir()),
            },
            "services": {
                "web_running": bool(self._server_thread.is_alive()),
                "web_process": self._server_thread.status() if isinstance(self._server_thread, ManagedRuntimeProcess) else None,
                "local_agent_api_enabled": bool(getattr(settings, "agent_api_enabled", False)),
                "local_agent_api_running": bool(
                    getattr(settings, "agent_api_enabled", False)
                    and (self._agent_api_thread is None or self._agent_api_thread.is_alive())
                ),
                "local_agent_api_reused": bool(
                    getattr(settings, "agent_api_enabled", False) and self._agent_api_thread is None
                ),
                "worker_running": bool(self._worker_thread and self._worker_thread.is_alive()),
                "worker_processes": self._worker_thread.status() if self._worker_thread is not None else None,
                "local_agent_api_process": self._agent_api_thread.status() if self._agent_api_thread is not None else None,
            },
            "gaming_mode": read_gaming_mode_state() or {"enabled": False},
            "updater": self.updater_status(fetch=False),
            "crash_reports": crash_status_payload(),
            "window": {
                "visible": window_visible,
                "close_to_tray": prefs.close_to_tray,
            },
        }

    def updater_status(self, *, fetch: bool = False) -> dict[str, object]:
        with self._lock:
            running = bool(self._update_thread and self._update_thread.is_alive())
            cached = dict(self._update_state)
        if running and not fetch:
            cached["running"] = True
            return cached
        if not fetch:
            cached["running"] = False
            return cached
        status = check_local_update(fetch=True)
        with self._lock:
            if not bool(self._update_thread and self._update_thread.is_alive()):
                self._update_state = dict(status)
            out = dict(self._update_state)
            out["running"] = bool(self._update_thread and self._update_thread.is_alive())
            return out

    def update_preferences(self, patch: dict[str, object]) -> dict[str, object]:
        current = self.current_preferences()
        updated = current.apply_patch(patch)
        if updated.launch_at_login != current.launch_at_login:
            try:
                set_launch_at_login_enabled(
                    updated.launch_at_login,
                    command=desktop_command(hidden=updated.start_minimized),
                )
            except Exception as exc:
                raise AutostartError(str(exc)) from exc
        self._pref_store.save(updated)
        return self.status_payload()

    def perform_action(self, action: str) -> dict[str, object]:
        normalized = str(action or "").strip().lower()
        if normalized == "start_harness":
            from thoth_terminal.web.deepseek_harness_service import harness_service
            return {"harness": harness_service.start_owned_application()}
        if normalized in {"manage_ibkr_credentials", "sign_in_ibkr"}:
            self.ibkr_action_requested.emit(normalized)
            return self.status_payload()
        if normalized == "show_window":
            self.show_window_requested.emit()
            return self.status_payload()
        if normalized == "hide_window":
            self.hide_window_requested.emit()
            return self.status_payload()
        if normalized == "quit":
            payload = self.status_payload()
            self.request_quit()
            return payload
        if normalized == "open_data_dir":
            open_path_in_file_manager(Path(settings.data_dir))
            return self.status_payload()
        if normalized == "open_logs_dir":
            open_path_in_file_manager(desktop_logs_dir())
            return self.status_payload()
        if normalized in {"open_crash_reports_dir", "open_crashes_dir"}:
            open_path_in_file_manager(crash_reports_dir())
            return self.status_payload()
        if normalized in {"create_support_bundle", "support_bundle"}:
            bundle = create_support_bundle(reason="desktop_action")
            payload = self.status_payload()
            payload["support_bundle"] = bundle
            return payload
        if normalized in {"open_support_bundles_dir", "open_support_dir"}:
            from thoth_terminal.crash_reports import support_bundles_dir

            open_path_in_file_manager(support_bundles_dir())
            return self.status_payload()
        if normalized in {"check_update", "check_for_updates"}:
            payload = self.status_payload()
            payload["updater"] = self.updater_status(fetch=True)
            return payload
        if normalized in {"apply_update_restart", "update_and_restart"}:
            return self._start_update_restart()
        if normalized in {"restart_desktop", "restart_thoth", "restart"}:
            return self._start_desktop_restart()
        if normalized in {"enter_gaming_mode", "gaming_mode", "performance_mode"}:
            return self._start_gaming_mode()
        if normalized in {"repair_desktop_integration", "repair_desktop", "repair_integration"}:
            return self._start_desktop_repair()
        if normalized == "open_ibkr_auth" and self.status_payload()["ibkr_sign_in"]["credentials_saved"]:
            self.ibkr_action_requested.emit("sign_in_ibkr")
            return self.status_payload()
        if normalized in {"open_ibkr_auth", "open_ibkr_gateway"}:
            gateway_payload = launch_gateway_if_needed(logs_dir=desktop_logs_dir())
            if not gateway_payload.get("ok"):
                raise RuntimeError(f"IBKR Gateway failed to start: {gateway_payload}")
            if gateway_payload.get("ready") is False:
                logger.warning("IBKR Gateway launch did not report ready before opening browser: %s", gateway_payload)
            QDesktopServices.openUrl(QUrl(gateway_browser_login_url()))
            payload = self.status_payload()
            payload["ibkr_gateway"] = gateway_payload
            return payload
        raise ValueError(f"Unknown desktop action: {action}")

    @Slot(str)
    def _handle_ibkr_action(self, action: str) -> None:
        from thoth_terminal.desktop.ibkr_sign_in import IbkrSignInManager

        if self._ibkr_sign_in is None:
            self._ibkr_sign_in = IbkrSignInManager(self, self._window)
        if action == "manage_ibkr_credentials":
            self._ibkr_sign_in.manage_credentials()
        else:
            self._ibkr_sign_in.sign_in(automatic=action == "sign_in_ibkr_on_launch")

    def _start_update_restart(self) -> dict[str, object]:
        with self._lock:
            if self._update_thread is not None and self._update_thread.is_alive():
                payload = self.status_payload()
                payload["updater"] = dict(self._update_state) | {"running": True}
                return payload
            self._update_state = {
                "state": "applying",
                "running": True,
                "started_at": _utc_now_iso(),
                "message": "Updating local checkout and preparing restart...",
            }
            self._update_thread = threading.Thread(
                target=self._apply_update_restart_worker,
                name="thoth-desktop-updater",
                daemon=True,
            )
            self._update_thread.start()
        payload = self.status_payload()
        payload["updater"] = self.updater_status(fetch=False)
        return payload

    def _start_gaming_mode(self) -> dict[str, object]:
        root = repo_root().resolve(strict=False)
        script = root / "scripts" / "enter-performance-mode.ps1"
        if not script.exists():
            raise RuntimeError(f"Missing performance-mode script: {script}")
        logs_dir = desktop_logs_dir()
        logs_dir.mkdir(parents=True, exist_ok=True)
        handle = start_hidden_logged_process(
            [
                "powershell.exe",
                "-NoProfile",
                "-WindowStyle",
                "Hidden",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-Reason",
                "desktop_tray",
            ],
            label="enter-performance-mode",
            cwd=str(root),
            logs_dir=logs_dir,
            detached=True,
            breakaway=True,
        )
        payload = self.status_payload()
        payload["gaming_mode"] = {
            "enabled": True,
            "script_pid": handle.pid,
            "stdout_log": str(handle.stdout_log),
            "stderr_log": str(handle.stderr_log),
        }
        self.request_quit()
        return payload

    def _start_desktop_restart(self) -> dict[str, object]:
        with self._lock:
            if self._update_thread is not None and self._update_thread.is_alive():
                payload = self.status_payload()
                payload["updater"] = dict(self._update_state) | {"running": True}
                return payload
            self._update_state = {
                "state": "restarting",
                "running": True,
                "started_at": _utc_now_iso(),
                "message": "Restarting Thoth Terminal to load local source changes...",
            }
            self._update_thread = threading.Thread(
                target=self._restart_desktop_worker,
                name="thoth-desktop-restart",
                daemon=True,
            )
            self._update_thread.start()
        payload = self.status_payload()
        payload["updater"] = self.updater_status(fetch=False)
        return payload

    def _start_desktop_repair(self) -> dict[str, object]:
        with self._lock:
            if self._update_thread is not None and self._update_thread.is_alive():
                payload = self.status_payload()
                payload["updater"] = dict(self._update_state) | {"running": True}
                return payload
            self._update_state = {
                "state": "repairing",
                "running": True,
                "started_at": _utc_now_iso(),
                "message": "Repairing desktop shortcuts, icon, and Python dependencies...",
            }
            self._update_thread = threading.Thread(
                target=self._repair_desktop_integration_worker,
                name="thoth-desktop-repair",
                daemon=True,
            )
            self._update_thread.start()
        payload = self.status_payload()
        payload["updater"] = self.updater_status(fetch=False)
        return payload

    def _set_update_state(self, state: dict[str, object]) -> None:
        with self._lock:
            self._update_state = dict(state)

    def _repair_desktop_integration_worker(self) -> None:
        try:
            prefs = self.current_preferences()
            logger.info("desktop integration repair started")
            result = repair_desktop_integration(launch_at_login=bool(prefs.launch_at_login), launch=False)
            self._set_update_state(
                {
                    "state": "repair_complete",
                    "running": False,
                    "checked_at": _utc_now_iso(),
                    "update_available": False,
                    "can_update": False,
                    "message": "Desktop integration repaired. Start Menu and Desktop shortcuts are ready.",
                    "result": result,
                }
            )
        except Exception as exc:
            logger.exception("desktop integration repair failed: %s", exc)
            self._set_update_state(
                {
                    "state": "error",
                    "running": False,
                    "checked_at": _utc_now_iso(),
                    "error": f"{type(exc).__name__}: {exc}",
                    "update_available": False,
                    "can_update": False,
                }
            )

    def _restart_desktop_worker(self) -> None:
        try:
            logger.info("desktop restart requested")
            self._set_update_state(
                {
                    "state": "restarting",
                    "running": True,
                    "checked_at": _utc_now_iso(),
                    "message": "Restarting Thoth Terminal...",
                }
            )
            self._launch_restart_helper()
            time.sleep(0.35)
            self.request_quit()
        except Exception as exc:
            logger.exception("desktop restart failed: %s", exc)
            self._set_update_state(
                {
                    "state": "error",
                    "running": False,
                    "checked_at": _utc_now_iso(),
                    "error": f"{type(exc).__name__}: {exc}",
                    "update_available": False,
                    "can_update": False,
                }
            )

    def _apply_update_restart_worker(self) -> None:
        try:
            logger.info("desktop updater started")
            result = apply_local_update(python_exe=sys.executable, reinstall=True)
            if not result.get("updated"):
                self._set_update_state(
                    {
                        "state": "current",
                        "running": False,
                        "checked_at": _utc_now_iso(),
                        "update_available": False,
                        "can_update": False,
                        "message": "No update was available.",
                        "result": result,
                    }
                )
                return
            self._set_update_state(
                {
                    "state": "restarting",
                    "running": True,
                    "checked_at": _utc_now_iso(),
                    "message": "Update installed. Restarting Thoth Terminal...",
                    "result": result,
                }
            )
            self._launch_restart_helper()
            time.sleep(0.35)
            self.request_quit()
        except Exception as exc:
            logger.exception("desktop updater failed: %s", exc)
            self._set_update_state(
                {
                    "state": "error",
                    "running": False,
                    "checked_at": _utc_now_iso(),
                    "error": f"{type(exc).__name__}: {exc}",
                    "update_available": False,
                    "can_update": False,
                }
            )

    def _launch_restart_helper(self) -> None:
        root = repo_root().resolve(strict=False)
        logs_dir = desktop_logs_dir()
        logs_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        helper_log = logs_dir / f"desktop-restart-{stamp}.helper.log"
        child_stdout = logs_dir / f"desktop-restart-{stamp}.out.log"
        child_stderr = logs_dir / f"desktop-restart-{stamp}.err.log"
        # Relaunches are initiated by a background helper after this Qt process
        # exits.  Keep the command hidden even on the degraded fallback path
        # where desktop_command() has to use console-bound python.exe.
        command = desktop_command(hidden=True)
        helper_code = (
            "import ctypes, json, os, subprocess, sys, time, traceback\n"
            "log_path = os.environ['THOTH_RESTART_HELPER_LOG']\n"
            "def log(message):\n"
            "    stamp = time.strftime('%Y-%m-%d %H:%M:%S')\n"
            "    with open(log_path, 'a', encoding='utf-8') as handle:\n"
            "        handle.write(f'{stamp} {message}\\n')\n"
            "def pid_alive(pid):\n"
            "    try:\n"
            "        pid = int(pid)\n"
            "    except Exception:\n"
            "        return False\n"
            "    if pid <= 0:\n"
            "        return False\n"
            "    if os.name == 'nt':\n"
            "        kernel32 = ctypes.windll.kernel32\n"
            "        handle = kernel32.OpenProcess(0x00100000, False, pid)\n"
            "        if not handle:\n"
            "            return False\n"
            "        try:\n"
            "            return int(kernel32.WaitForSingleObject(handle, 0)) == 0x00000102\n"
            "        finally:\n"
            "            kernel32.CloseHandle(handle)\n"
            "    try:\n"
            "        os.kill(pid, 0)\n"
            "        return True\n"
            "    except Exception:\n"
            "        return False\n"
            "def creationflags(breakaway=True):\n"
            "    if os.name != 'nt':\n"
            "        return 0\n"
            "    flags = getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0) | getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NO_WINDOW', 0)\n"
            "    if breakaway:\n"
            "        flags |= getattr(subprocess, 'CREATE_BREAKAWAY_FROM_JOB', 0)\n"
            "    return flags\n"
            "try:\n"
            "    cmd = json.loads(os.environ['THOTH_RESTART_CMD'])\n"
            "    cwd = os.environ['THOTH_RESTART_CWD']\n"
            "    parent_pid = int(os.environ.get('THOTH_RESTART_PARENT_PID') or '0')\n"
            "    out_path = os.environ['THOTH_RESTART_STDOUT']\n"
            "    err_path = os.environ['THOTH_RESTART_STDERR']\n"
            "    deadline = time.time() + float(os.environ.get('THOTH_RESTART_PARENT_TIMEOUT_S', '75.0'))\n"
            "    log(f'helper started parent_pid={parent_pid} cmd={cmd!r}')\n"
            "    while parent_pid and pid_alive(parent_pid) and time.time() < deadline:\n"
            "        time.sleep(0.5)\n"
            "    if parent_pid and pid_alive(parent_pid):\n"
            "        log(f'parent still alive after timeout parent_pid={parent_pid}; launching anyway')\n"
            "    else:\n"
            "        log(f'parent exited parent_pid={parent_pid}')\n"
            "    time.sleep(float(os.environ.get('THOTH_RESTART_DELAY_S', '0.75')))\n"
            "    last_error = None\n"
            "    for attempt in range(1, 4):\n"
            "        try:\n"
            "            with open(out_path, 'ab') as out, open(err_path, 'ab') as err:\n"
            "                child = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=err, creationflags=creationflags(breakaway=True))\n"
            "            log(f'launched child pid={child.pid} attempt={attempt}')\n"
            "            break\n"
            "        except OSError as exc:\n"
            "            last_error = exc\n"
            "            log(f'launch attempt {attempt} failed with breakaway: {exc!r}')\n"
            "            try:\n"
            "                with open(out_path, 'ab') as out, open(err_path, 'ab') as err:\n"
            "                    child = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=err, creationflags=creationflags(breakaway=False))\n"
            "                log(f'launched child pid={child.pid} attempt={attempt} fallback=true')\n"
            "                break\n"
            "            except Exception as fallback_exc:\n"
            "                last_error = fallback_exc\n"
            "                log(f'launch attempt {attempt} fallback failed: {fallback_exc!r}')\n"
            "                time.sleep(2.0)\n"
            "    else:\n"
            "        raise RuntimeError(f'failed to relaunch desktop: {last_error!r}')\n"
            "except Exception:\n"
            "    log('helper failed:\\n' + traceback.format_exc())\n"
            "    raise\n"
        )
        env = os.environ.copy()
        env.update(
            {
                "THOTH_RESTART_CMD": json.dumps([str(part) for part in command]),
                "THOTH_RESTART_CWD": str(root),
                "THOTH_RESTART_HELPER_LOG": str(helper_log),
                "THOTH_RESTART_PARENT_PID": str(os.getpid()),
                "THOTH_RESTART_PARENT_TIMEOUT_S": "75.0",
                "THOTH_RESTART_STDOUT": str(child_stdout),
                "THOTH_RESTART_STDERR": str(child_stderr),
                "THOTH_RESTART_DELAY_S": "0.75",
            }
        )
        helper_log.parent.mkdir(parents=True, exist_ok=True)
        try:
            start_hidden_logged_process(
                [sys.executable, "-c", helper_code],
                label="desktop-restart-helper",
                cwd=str(root),
                env=env,
                stdout_path=helper_log,
                stderr_path=helper_log,
                detached=True,
                breakaway=True,
            )
        except OSError:
            start_hidden_logged_process(
                [sys.executable, "-c", helper_code],
                label="desktop-restart-helper",
                cwd=str(root),
                env=env,
                stdout_path=helper_log,
                stderr_path=helper_log,
                detached=True,
                breakaway=False,
            )
        logger.info(
            "desktop restart helper launched command=%s helper_log=%s stdout=%s stderr=%s",
            command,
            helper_log,
            child_stdout,
            child_stderr,
        )

    def hide_to_tray(self) -> None:
        self.hide_window_requested.emit()
        with self._lock:
            tray = self._tray
        if tray is not None and (not self._tray_hint_shown):
            tray.showMessage(
                WINDOW_TITLE,
                "Thoth is still running in the system tray.",
                QSystemTrayIcon.Information,
                2500,
            )
            self._tray_hint_shown = True

    def request_quit(self) -> None:
        _force_process_exit_later()
        self.quit_requested.emit()

    def shutdown_services(self) -> None:
        if self._ibkr_sign_in is not None:
            self._ibkr_sign_in.shutdown()
        if self._tunnel_process is not None:
            try:
                self._tunnel_process.stop()
                _remove_runtime_state_file("cloudflared")
            except Exception:
                logger.warning("cloudflared tunnel shutdown failed", exc_info=True)
        if self._worker_thread is not None:
            self._worker_thread.stop()
        if self._agent_api_thread is not None:
            self._agent_api_thread.stop()
        self._server_thread.stop()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=5)
            if self._worker_thread.is_alive():
                logger.warning("desktop worker thread still alive after shutdown grace period")
        if self._agent_api_thread is not None:
            self._agent_api_thread.join(timeout=5)
            if self._agent_api_thread.is_alive():
                logger.warning("desktop local agent API thread still alive after shutdown grace period")
        self._server_thread.join(timeout=5)
        if self._server_thread.is_alive():
            logger.warning("desktop web server thread still alive after shutdown grace period")
        _shutdown_playwright_connectors()
        payload = stop_managed_gateway()
        if not payload.get("ok"):
            logger.warning("IBKR gateway shutdown failed: %s", payload)
        else:
            logger.info("IBKR gateway shutdown complete: %s", payload)
        terminate_thoth_support_processes()
        _stop_desktop_local_postgres()

    @Slot()
    def _show_window(self) -> None:
        with self._lock:
            window = self._window
        if window is None:
            return
        window.show()
        window.raise_()
        window.activateWindow()
        self.set_window_visible(True)

    @Slot()
    def _hide_window(self) -> None:
        with self._lock:
            window = self._window
        if window is None:
            return
        window.hide()
        self.set_window_visible(False)

    @Slot()
    def _quit_application(self) -> None:
        app = QApplication.instance()
        if app is not None:
            app.quit()


class ThothWebPage(QWebEnginePage):
    def __init__(self, controller: DesktopController, base_url: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._base_url = QUrl(base_url.rstrip("/"))

    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:
        if url.scheme() in {"http", "https"}:
            is_local = (
                url.host() == self._base_url.host()
                and int(url.port()) == int(self._base_url.port())
            )
            if (not is_local) and self._controller.current_preferences().open_external_links_in_browser:
                QDesktopServices.openUrl(url)
                return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)

    def createWindow(self, window_type):  # type: ignore[override]
        popup = QWebEnginePage(self)

        def _open_external(url: QUrl) -> None:
            if url.isValid() and url.scheme() in {"http", "https"}:
                QDesktopServices.openUrl(url)
            popup.deleteLater()

        popup.urlChanged.connect(_open_external)
        return popup


class ThothMainWindow(QMainWindow):
    def __init__(self, controller: DesktopController, base_url: str) -> None:
        super().__init__()
        self._controller = controller
        self._base_url = str(base_url).rstrip("/")
        self._load_failure_reported = False
        self.setWindowTitle(WINDOW_TITLE)
        self.setWindowIcon(_app_icon())
        self.resize(1480, 980)

        self.web_view = QWebEngineView(self)
        self.web_view.setPage(ThothWebPage(controller, base_url, self.web_view))
        self.web_view.loadFinished.connect(self._on_load_finished)
        try:
            self.web_view.renderProcessTerminated.connect(self._on_render_process_terminated)
        except Exception:
            logger.debug("QWebEngine renderProcessTerminated signal unavailable", exc_info=True)
        self.web_view.load(QUrl(base_url))
        self.setCentralWidget(self.web_view)

    def _on_load_finished(self, ok: bool) -> None:
        if ok:
            return
        url = self.web_view.url().toString()
        update_desktop_session(phase="webview_load_failed", last_url=url, webview_load_ok=False)
        if not self._load_failure_reported:
            self._load_failure_reported = True
            write_crash_report(
                "desktop_webview_load_failed",
                summary=f"Desktop webview failed to load {url or self._base_url}",
                context={"url": url, "base_url": self._base_url},
                severity="error",
            )

    def _on_render_process_terminated(self, termination_status, exit_code: int) -> None:
        report = write_crash_report(
            "desktop_webview_render_process_terminated",
            summary=f"Qt WebEngine render process terminated with exit code {exit_code}.",
            context={
                "termination_status": str(termination_status),
                "exit_code": int(exit_code),
                "url": self.web_view.url().toString(),
                "base_url": self._base_url,
            },
            severity="fatal",
        )
        update_desktop_session(
            status="crashed",
            phase="webview_render_process_terminated",
            last_crash_report=report,
            last_url=self.web_view.url().toString(),
        )

    def showEvent(self, event) -> None:  # type: ignore[override]
        self._controller.set_window_visible(True)
        super().showEvent(event)

    def hideEvent(self, event) -> None:  # type: ignore[override]
        self._controller.set_window_visible(False)
        super().hideEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._controller.current_preferences().close_to_tray and self._controller.tray_available():
            event.ignore()
            self._controller.hide_to_tray()
            return
        super().closeEvent(event)


def _build_tray(controller: DesktopController) -> QSystemTrayIcon | None:
    if not QSystemTrayIcon.isSystemTrayAvailable():
        return None
    tray = QSystemTrayIcon(_app_icon())
    tray.setToolTip(WINDOW_TITLE)

    menu = QMenu()
    open_action = QAction("Open Thoth", tray)
    open_action.triggered.connect(lambda _checked=False: controller.show_window_requested.emit())
    menu.addAction(open_action)

    data_action = QAction("Open Data Folder", tray)
    data_action.triggered.connect(lambda _checked=False: open_path_in_file_manager(Path(settings.data_dir)))
    menu.addAction(data_action)

    logs_action = QAction("Open Logs", tray)
    logs_action.triggered.connect(lambda _checked=False: open_path_in_file_manager(desktop_logs_dir()))
    menu.addAction(logs_action)

    crashes_action = QAction("Open Crash Reports", tray)
    crashes_action.triggered.connect(lambda _checked=False: open_path_in_file_manager(crash_reports_dir()))
    menu.addAction(crashes_action)

    menu.addSeparator()
    gaming_action = QAction("Enter Gaming Mode", tray)
    gaming_action.triggered.connect(lambda _checked=False: controller.perform_action("enter_gaming_mode"))
    menu.addAction(gaming_action)

    menu.addSeparator()
    quit_action = QAction("Quit", tray)
    quit_action.triggered.connect(lambda _checked=False: controller.request_quit())
    menu.addAction(quit_action)

    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: controller.show_window_requested.emit()
        if reason in {QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick}
        else None
    )
    tray.show()
    return tray


def _handle_existing_instance(qt_app: QApplication, *, start_hidden: bool = False) -> bool:
    existing = read_existing_instance()
    mutex_available = acquire_single_instance_mutex()
    if existing is None and mutex_available:
        return True
    if start_hidden:
        if existing is not None:
            logger.info("desktop hidden launch found existing instance pid=%s; exiting without prompt", existing.pid)
        else:
            logger.info("desktop hidden launch found existing mutex owner; exiting without prompt")
        qt_app.quit()
        return False
    detail = "Another Thoth Terminal process is starting or already running."
    if existing is not None:
        detail = f"Existing process: PID {existing.pid}"
    if existing is not None and existing.base_url:
        detail += f"\nLocal URL: {existing.base_url}"
    box = QMessageBox()
    box.setWindowTitle(WINDOW_TITLE)
    box.setWindowIcon(_app_icon())
    box.setIcon(QMessageBox.Question)
    box.setText("Thoth Terminal is already running.")
    box.setInformativeText(
        "Do you want to restart and reopen Thoth Terminal?\n\n"
        "Choose Restart if the existing window is hung or left over from a previous launch."
    )
    box.setDetailedText(detail)
    restart_button = box.addButton("Restart and Reopen", QMessageBox.AcceptRole)
    open_button = box.addButton("Open Existing", QMessageBox.ActionRole)
    cancel_button = box.addButton("Cancel", QMessageBox.RejectRole)
    box.setDefaultButton(open_button)
    box.exec()
    clicked = box.clickedButton()
    if clicked == restart_button:
        if existing is not None:
            terminate_instance(existing)
        else:
            terminate_desktop_processes()
        payload = stop_managed_gateway()
        if not payload.get("ok"):
            logger.warning("IBKR gateway shutdown after desktop restart request failed: %s", payload)
        time.sleep(0.75)
        return acquire_single_instance_mutex()
    if clicked == open_button:
        if existing is not None and existing.base_url:
            try:
                requests.post(
                    f"{existing.base_url.rstrip('/')}/api/desktop/action",
                    json={"action": "show_window"},
                    timeout=2.5,
                )
            except Exception:
                QDesktopServices.openUrl(QUrl(existing.base_url))
        return False
    if clicked == cancel_button:
        return False
    qt_app.quit()
    return False


def launch_desktop_app(*, start_hidden: bool = False, start_worker: bool = True) -> None:
    crash_context: dict[str, Any] = {
        "phase": "process_start",
        "start_hidden": bool(start_hidden),
        "start_worker_requested": bool(start_worker),
    }
    session_id: str | None = None
    clean_exit = False
    startup: StartupWindow | None = None
    window: ThothMainWindow | None = None
    tray: QSystemTrayIcon | None = None
    web_app: Any | None = None
    server_thread: ManagedWebProcess | None = None
    agent_api_thread: ManagedLocalAgentApiProcess | None = None
    worker_thread: ManagedWorkerProcess | None = None
    local_model_payload: dict[str, object] | None = None
    gateway_payload: dict[str, object] | None = None
    tunnel_process: DesktopTunnelProcess | None = None
    control_server: DesktopControlServer | None = None
    support_supervisor: SupportSupervisor | None = None
    local_postgres_started_by_desktop = False

    def _set_phase(phase: str, **extra: Any) -> None:
        crash_context.update({"phase": phase, **extra})
        update_desktop_session(phase=phase, **extra)

    try:
        configure_desktop_runtime()
        _configure_desktop_file_logging()
        gaming_state = read_gaming_mode_state()
        if gaming_state and bool(gaming_state.get("enabled", True)):
            logger.info("desktop launch suppressed because Gaming Mode is active: %s", gaming_state)
            clean_exit = True
            return
        session_payload = begin_desktop_session(crash_context | {"phase": "runtime_configured"})
        session_id = str(session_payload.get("session_id") or "")
        install_crash_hooks(lambda: dict(crash_context))
        logger.info("desktop launch requested hidden=%s worker=%s", start_hidden, start_worker)
        _set_phase("qapplication_start")
        set_windows_process_identity()
        qt_app = QApplication.instance() or QApplication([])
        logger.info("desktop QApplication ready")
        qt_app.setApplicationDisplayName(WINDOW_TITLE)
        qt_app.setApplicationName("ThothTerminal")
        qt_app.setQuitOnLastWindowClosed(False)
        qt_app.setWindowIcon(_app_icon())

        logger.info("desktop checking existing instance")
        _set_phase("existing_instance_check")
        if not _handle_existing_instance(qt_app, start_hidden=start_hidden):
            logger.info("desktop launch stopped by existing-instance handler")
            clean_exit = True
            return
        logger.info("desktop existing-instance check passed")

        _set_phase("instance_lock_write")
        write_instance_lock(base_url=None)
        startup = StartupWindow()
        if not start_hidden:
            startup.show()
            qt_app.processEvents()

        logger.info("desktop deferring optional integrations until requested")
        logger.info("desktop loading preferences")
        _set_phase("preferences_load")
        pref_store = DesktopPreferenceStore()
        prefs = pref_store.load()
        logger.info("desktop validating security posture")
        _set_phase("security_posture_validate")
        startup.set_message("Validating desktop security posture...")
        host = "127.0.0.1"
        validate_security_posture(settings, bind_host=host)
        port = _pick_available_port(host, int(settings.port))
        base_url = f"http://{host}:{port}"
        crash_context["base_url"] = base_url
        worker_enabled = bool(start_worker and prefs.start_worker_on_launch)
        agent_api_enabled = bool(prefs.start_agent_api_on_launch)
        agent_api_host = str(getattr(settings, "agent_api_host", "0.0.0.0") or "0.0.0.0")
        agent_api_port = int(getattr(settings, "agent_api_port", 8765) or 8765)
        agent_api_probe_host = "127.0.0.1" if agent_api_host in {"0.0.0.0", "::"} else agent_api_host
        agent_api_base_url = f"http://{agent_api_probe_host}:{agent_api_port}"
        settings.agent_api_enabled = agent_api_enabled
        settings.agent_api_token = _resolve_agent_api_token()
        settings.agent_api_host = agent_api_host
        settings.agent_api_port = agent_api_port

        logger.info("desktop preparing database")
        _set_phase("database_prepare", base_url=base_url)
        startup.set_message("Preparing local database...")
        local_postgres_started_by_desktop = _ensure_desktop_postgres_ready()
        init_db(allow_locked=True)

        logger.info("desktop preparing isolated web process at %s", base_url)
        _set_phase("web_server_start", base_url=base_url)
        startup.set_message("Starting local web server...")
        control_server = DesktopControlServer(None)
        control_server.start()
        # Every child that can request Harness startup must use the desktop
        # owner. A worker-owned launch inherits its kill-on-close Windows job.
        settings.desktop_control_url = control_server.base_url
        settings.desktop_control_token = control_server.token
        server_thread = ManagedWebProcess(
            host=host,
            port=port,
            control_url=control_server.base_url,
            control_token=control_server.token,
        )
        agent_api_thread: ManagedLocalAgentApiProcess | None = None
        if not agent_api_enabled:
            logger.warning("desktop local agent API disabled by launch preferences")
            startup.set_message("Local agent API disabled for this launch...")
        elif _prepare_local_agent_api_start(agent_api_base_url, bind_host=agent_api_host, bind_port=agent_api_port):
            logger.info("desktop reusing existing local agent API at %s", agent_api_base_url)
            startup.set_message("Reusing local agent API...")
        else:
            logger.info("desktop starting local agent API at http://%s:%s", agent_api_host, agent_api_port)
            startup.set_message("Starting local agent API...")
            agent_api_thread = ManagedLocalAgentApiProcess(host=agent_api_host, port=agent_api_port)
        worker_thread = ManagedWorkerProcess() if worker_enabled else None
        controller = DesktopController(
            pref_store=pref_store,
            base_url=base_url,
            server_thread=server_thread,
            agent_api_thread=agent_api_thread,
            worker_thread=worker_thread,
            local_postgres_started_by_desktop=local_postgres_started_by_desktop,
        )
        control_server.controller = controller

        server_thread.start()
        if agent_api_thread is not None:
            agent_api_thread.start()
        try:
            _set_phase("web_health_wait", base_url=base_url)
            startup.set_message("Waiting for terminal health check...")
            _wait_for_server(base_url, server_thread=server_thread)
            if agent_api_enabled:
                _set_phase("agent_api_health_wait", agent_api_base_url=agent_api_base_url)
                startup.set_message("Waiting for local agent API...")
                agent_api_ready = _wait_for_local_agent_api(
                    agent_api_base_url,
                    server_thread=agent_api_thread,
                    timeout_s=8.0,
                    required=False,
                )
                if not agent_api_ready:
                    startup.set_message("Local agent API is still starting; opening terminal...")
        except Exception as exc:
            write_crash_report(
                "desktop_services_start_failed",
                summary=str(exc),
                exc_info=sys.exc_info(),
                context={"base_url": base_url, "agent_api_base_url": agent_api_base_url},
            )
            raise

        if worker_thread is not None:
            _set_phase("worker_start")
            startup.set_message("Starting background worker...")
            worker_thread.start()

        _set_phase("runtime_state_write", base_url=base_url)
        write_desktop_runtime_state(
            base_url=base_url,
            worker_enabled=worker_enabled,
            agent_api_base_url=agent_api_base_url,
            agent_api_enabled=agent_api_enabled,
            web_pid=server_thread.pid,
            worker_pids=[child.pid for child in worker_thread.children if child.pid] if worker_thread is not None else [],
            agent_api_pid=agent_api_thread.pid if agent_api_thread is not None else None,
        )
        write_instance_lock(base_url=base_url)
        tunnel_process = _maybe_start_desktop_tunnel(base_url)
        controller.attach_tunnel(tunnel_process)

        # The watchdog is the lightweight owner of persistent integrations such
        # as Discord. Keep only its self-repair loop here; expensive optional
        # integrations remain demand-loaded by their request paths.
        support_supervisor = SupportSupervisor(repair_tasks=[])
        support_supervisor.start()

        _set_phase("window_create", base_url=base_url)
        startup.set_message("Opening command surface...")
        window = ThothMainWindow(controller, base_url)
        tray = _build_tray(controller)
        controller.attach_window(window, tray)

        def _shutdown() -> None:
            _set_phase("shutdown_started")
            try:
                if support_supervisor is not None:
                    support_supervisor.stop()
                controller.shutdown_services()
            except Exception:
                logger.exception("desktop shutdown services failed")
                write_crash_report(
                    "desktop_shutdown_exception",
                    summary="Desktop shutdown services failed.",
                    exc_info=sys.exc_info(),
                    severity="error",
                )
            finally:
                if control_server is not None:
                    control_server.stop()
                clear_instance_lock()
                _set_phase("shutdown_complete")

        qt_app.aboutToQuit.connect(_shutdown)

        startup.set_message("Checking independent storage recovery...")
        qt_app.processEvents()
        storage_recovery = _storage_recovery_readiness()

        should_hide = bool(start_hidden or prefs.start_minimized)
        if should_hide and tray is not None:
            window.hide()
        else:
            window.show()
        startup.close()

        if storage_recovery.get("applicable") and not storage_recovery.get("ok"):
            reason = str(storage_recovery.get("reason") or "status_unavailable")
            logger.error("storage recovery scheduled task is not armed: %s", reason)
            if tray is not None:
                tray.showMessage(
                    "Storage recovery needs setup",
                    "The installed storage recovery task is not armed. Check Thoth runtime status and install the storage recovery broker.",
                    QSystemTrayIcon.Warning,
                    15000,
                )

        logger.info("desktop startup complete: %s (optional integrations demand-loaded)", base_url)
        if prefs.ibkr_sign_in_on_launch and settings.enable_ibkr and not settings.ibkr_mock_enabled:
            QTimer.singleShot(0, lambda: controller.ibkr_action_requested.emit("sign_in_ibkr_on_launch"))
        _set_phase("qt_event_loop", base_url=base_url)
        qt_app.exec()
        clean_exit = True
    except Exception as exc:
        exc_info = sys.exc_info()
        if support_supervisor is not None:
            support_supervisor.stop()
        if not clean_exit:
            _cleanup_failed_desktop_launch(
                startup=startup,
                window=window,
                tray=tray,
                web_app=web_app,
                server_thread=server_thread,
                agent_api_thread=agent_api_thread,
                worker_thread=worker_thread,
                control_server=control_server,
                local_model_payload=local_model_payload,
                gateway_payload=gateway_payload,
                tunnel_process=tunnel_process,
                local_postgres_started_by_desktop=local_postgres_started_by_desktop,
            )
        report = write_crash_report(
            "desktop_launch_exception",
            summary="Desktop launch failed.",
            exc_info=exc_info,
            context=crash_context,
            severity="fatal",
        )
        update_desktop_session(status="crashed", last_crash_report=report)
        _maybe_show_launch_failure_dialog(exc, start_hidden=start_hidden)
        raise
    finally:
        if clean_exit and session_id:
            mark_desktop_session_clean(session_id)
