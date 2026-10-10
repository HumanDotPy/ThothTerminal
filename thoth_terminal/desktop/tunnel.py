from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from thoth_terminal.config import settings
from thoth_terminal.desktop.runtime import desktop_logs_dir
from thoth_terminal.runtime_info import write_runtime_state
from thoth_terminal.runtime_processes import run_hidden_process, start_hidden_logged_process


DEFAULT_HOSTNAMES = (
    "thoth.agenticsystemsgroup.com",
    "thothterminal.agenticsystemsgroup.com",
    "thothmcp.agenticsystemsgroup.com",
)


@dataclass(frozen=True)
class DesktopTunnelProcess:
    pid: int
    config_path: Path
    stdout_log: Path
    stderr_log: Path

    def stop(self) -> None:
        _stop_process_tree(self.pid)


def tunnel_enabled() -> bool:
    return str(os.environ.get("THOTH_ENABLE_TUNNEL") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _cloudflared_executable() -> Path | None:
    found = shutil.which("cloudflared.exe") or shutil.which("cloudflared")
    if found:
        return Path(found)
    for candidate in (
        Path("C:/Program Files (x86)/cloudflared/cloudflared.exe"),
        Path("C:/Program Files/Cloudflare/cloudflared.exe"),
    ):
        if candidate.exists():
            return candidate
    return None


def _user_config_path() -> Path | None:
    for name in ("config.yml", "config.yaml"):
        candidate = Path.home() / ".cloudflared" / name
        if candidate.exists():
            return candidate
    return None


def _unquote(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _config_value(config_path: Path | None, key: str) -> str | None:
    if config_path is None or not config_path.exists():
        return None
    pattern = re.compile(r"^\s*" + re.escape(key) + r"\s*:\s*(.+?)\s*$")
    for line in config_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = pattern.match(line)
        if match:
            return _unquote(match.group(1))
    return None


def _config_hostnames(config_path: Path | None) -> list[str]:
    hostnames: set[str] = set()
    if config_path is not None and config_path.exists():
        pattern = re.compile(r"^\s*-\s*hostname\s*:\s*(.+?)\s*$")
        for line in config_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            match = pattern.match(line)
            if match:
                hostname = _unquote(match.group(1))
                if hostname:
                    hostnames.add(hostname)
    return sorted(hostnames or set(DEFAULT_HOSTNAMES))


def runtime_config_path() -> Path:
    return Path(settings.runtime_state_dir) / "cloudflared-thoth-runtime.yml"


def write_cloudflared_runtime_config(origin_base_url: str) -> Path | None:
    origin = str(origin_base_url or "").strip().rstrip("/")
    if not origin:
        return None
    source_config = _user_config_path()
    tunnel = _config_value(source_config, "tunnel")
    credentials_file = _config_value(source_config, "credentials-file")
    if not tunnel or not credentials_file:
        return None
    lines = [
        f"tunnel: {tunnel}",
        f"credentials-file: {credentials_file}",
        "ingress:",
    ]
    for hostname in _config_hostnames(source_config):
        lines.append(f"  - hostname: {hostname}")
        lines.append(f"    service: {origin}")
    lines.append("  - service: http_status:404")

    path = runtime_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _stop_process_tree(pid: int) -> None:
    try:
        root_pid = int(pid)
    except Exception:
        return
    if root_pid <= 0:
        return
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
        run_hidden_process(
            ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-Command", script],
            check=False,
            timeout_s=10.0,
        )
        return
    try:
        os.kill(root_pid, 15)
    except Exception:
        pass


def stop_existing_thoth_tunnels() -> None:
    if os.name != "nt":
        return
    script = r"""
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -match 'cloudflared' -and $_.CommandLine -match 'tunnel run thoth' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
"""
    run_hidden_process(
        ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-Command", script],
        check=False,
        timeout_s=10.0,
    )


def start_desktop_tunnel(origin_base_url: str) -> DesktopTunnelProcess | None:
    if not tunnel_enabled():
        return None
    cloudflared = _cloudflared_executable()
    if cloudflared is None:
        return None
    origin = str(origin_base_url or "").strip().rstrip("/")
    config_path = write_cloudflared_runtime_config(origin)
    if config_path is None:
        return None

    stop_existing_thoth_tunnels()
    logs_dir = desktop_logs_dir()
    logs_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stdout_log = logs_dir / f"cloudflared-desktop-{stamp}.out.log"
    stderr_log = logs_dir / f"cloudflared-desktop-{stamp}.err.log"
    handle = start_hidden_logged_process(
        [str(cloudflared), "--config", str(config_path), "tunnel", "run", "thoth"],
        label="cloudflared-desktop",
        cwd=str(Path.cwd()),
        stdout_path=stdout_log,
        stderr_path=stderr_log,
        detached=False,
    )
    write_runtime_state(
        service="cloudflared",
        data={
            "desktop_managed": True,
            "base_url": origin,
            "pid": int(handle.pid),
            "config_path": str(config_path),
            "stdout_log": str(stdout_log),
            "stderr_log": str(stderr_log),
        },
    )
    return DesktopTunnelProcess(
        pid=int(handle.pid),
        config_path=config_path,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
    )
