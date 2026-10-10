from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from thoth_terminal.runtime_env import detect_platform, repo_root


APP_RUN_NAME = "ThothTerminal"
MAC_AGENT_LABEL = "com.thothterminal.desktop"
WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


class AutostartError(RuntimeError):
    pass


def desktop_command(*, hidden: bool = True) -> list[str]:
    if getattr(sys, "frozen", False):
        command = [sys.executable]
    elif detect_platform() == "windows":
        root = repo_root()
        pythonw = root / ".venv" / "Scripts" / "pythonw.exe"
        launcher = root / "scripts" / "launch-thoth-desktop.pyw"
        if pythonw.exists() and launcher.exists():
            command = [str(pythonw), str(launcher)]
        else:
            command = [sys.executable, "-m", "thoth_terminal.cli", "desktop"]
    else:
        command = [sys.executable, "-m", "thoth_terminal.cli", "desktop"]
    if hidden:
        command.append("--hidden")
    return command


def is_supported() -> bool:
    return detect_platform() in {"windows", "mac"}


def is_enabled() -> bool:
    platform_kind = detect_platform()
    if platform_kind == "windows":
        return _windows_is_enabled()
    if platform_kind == "mac":
        return _mac_launch_agent_path().exists()
    return False


def set_enabled(enabled: bool, *, command: Sequence[str] | None = None) -> None:
    platform_kind = detect_platform()
    launch_command = list(command or desktop_command(hidden=True))
    if platform_kind == "windows":
        _windows_set_enabled(enabled, launch_command)
        return
    if platform_kind == "mac":
        _mac_set_enabled(enabled, launch_command)
        return
    raise AutostartError("Launch at login is currently supported on Windows and macOS only.")


def _windows_is_enabled() -> bool:
    try:
        import winreg
    except ImportError as exc:
        raise AutostartError("winreg is unavailable on this runtime.") from exc
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, APP_RUN_NAME)
            return bool(str(value).strip())
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AutostartError(f"Failed to inspect Windows startup entry: {exc}") from exc


def _windows_set_enabled(enabled: bool, command: Sequence[str]) -> None:
    try:
        import winreg
    except ImportError as exc:
        raise AutostartError("winreg is unavailable on this runtime.") from exc
    command_line = subprocess.list2cmdline([str(part) for part in command])
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            WINDOWS_RUN_KEY,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            if enabled:
                winreg.SetValueEx(key, APP_RUN_NAME, 0, winreg.REG_SZ, command_line)
            else:
                try:
                    winreg.DeleteValue(key, APP_RUN_NAME)
                except FileNotFoundError:
                    pass
    except OSError as exc:
        raise AutostartError(f"Failed to update Windows startup entry: {exc}") from exc


def _mac_launch_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MAC_AGENT_LABEL}.plist"


def _mac_set_enabled(enabled: bool, command: Sequence[str]) -> None:
    path = _mac_launch_agent_path()
    if not enabled:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise AutostartError(f"Failed to remove LaunchAgent: {exc}") from exc
        return
    payload = {
        "Label": MAC_AGENT_LABEL,
        "ProgramArguments": [str(part) for part in command],
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Interactive",
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            plistlib.dump(payload, handle, sort_keys=False)
    except OSError as exc:
        raise AutostartError(f"Failed to write LaunchAgent: {exc}") from exc
