from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from threading import RLock
from typing import Any, Mapping

from thoth_terminal.config import settings


def _coerce_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


@dataclass(slots=True, frozen=True)
class DesktopPreferences:
    launch_at_login: bool = False
    close_to_tray: bool = True
    start_minimized: bool = False
    start_worker_on_launch: bool = True
    use_local_models_for_ingestion: bool = True
    start_agent_api_on_launch: bool = True
    open_external_links_in_browser: bool = True
    ibkr_sign_in_on_launch: bool = False

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "DesktopPreferences":
        base = cls()
        if not payload:
            return base
        return replace(
            base,
            launch_at_login=_coerce_bool(payload.get("launch_at_login"), base.launch_at_login),
            close_to_tray=_coerce_bool(payload.get("close_to_tray"), base.close_to_tray),
            start_minimized=_coerce_bool(payload.get("start_minimized"), base.start_minimized),
            start_worker_on_launch=_coerce_bool(
                payload.get("start_worker_on_launch"), base.start_worker_on_launch
            ),
            use_local_models_for_ingestion=_coerce_bool(
                payload.get("use_local_models_for_ingestion"), base.use_local_models_for_ingestion
            ),
            start_agent_api_on_launch=_coerce_bool(
                payload.get("start_agent_api_on_launch"), base.start_agent_api_on_launch
            ),
            open_external_links_in_browser=_coerce_bool(
                payload.get("open_external_links_in_browser"),
                base.open_external_links_in_browser,
            ),
            ibkr_sign_in_on_launch=_coerce_bool(
                payload.get("ibkr_sign_in_on_launch"), base.ibkr_sign_in_on_launch,
            ),
        )

    def apply_patch(self, payload: Mapping[str, Any] | None) -> "DesktopPreferences":
        if not payload:
            return self
        return replace(
            self,
            launch_at_login=_coerce_bool(payload.get("launch_at_login"), self.launch_at_login),
            close_to_tray=_coerce_bool(payload.get("close_to_tray"), self.close_to_tray),
            start_minimized=_coerce_bool(payload.get("start_minimized"), self.start_minimized),
            start_worker_on_launch=_coerce_bool(
                payload.get("start_worker_on_launch"), self.start_worker_on_launch
            ),
            use_local_models_for_ingestion=_coerce_bool(
                payload.get("use_local_models_for_ingestion"), self.use_local_models_for_ingestion
            ),
            start_agent_api_on_launch=_coerce_bool(
                payload.get("start_agent_api_on_launch"), self.start_agent_api_on_launch
            ),
            open_external_links_in_browser=_coerce_bool(
                payload.get("open_external_links_in_browser"),
                self.open_external_links_in_browser,
            ),
            ibkr_sign_in_on_launch=_coerce_bool(
                payload.get("ibkr_sign_in_on_launch"), self.ibkr_sign_in_on_launch,
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def preferences_path(data_dir: Path | None = None) -> Path:
    return Path(data_dir or settings.data_dir) / "desktop" / "preferences.json"


def local_ingestion_enabled() -> bool:
    """Read the shared persistent preference in web and agent API processes."""
    return DesktopPreferenceStore().load().use_local_models_for_ingestion


class DesktopPreferenceStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or preferences_path())
        self._lock = RLock()

    def load(self) -> DesktopPreferences:
        with self._lock:
            if not self.path.exists():
                return DesktopPreferences()
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                return DesktopPreferences()
            if not isinstance(payload, dict):
                return DesktopPreferences()
            return DesktopPreferences.from_mapping(payload)

    def save(self, prefs: DesktopPreferences) -> DesktopPreferences:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(prefs.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return prefs
