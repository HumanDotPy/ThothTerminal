from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class IbkrConfig:
    enabled: bool = False
    mock_enabled: bool = False
    gateway_base_url: str = "https://localhost:5000/v1/api"
    verify_ssl: bool = False
    timeout_s: float = 12.0
    auth_backoff_s: float = 60.0
    default_account_id: str | None = None
    symbol_conids: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, settings: Any) -> "IbkrConfig":
        return cls(
            enabled=bool(getattr(settings, "enable_ibkr", False)),
            mock_enabled=bool(getattr(settings, "ibkr_mock_enabled", False)),
            gateway_base_url=str(
                getattr(settings, "ibkr_gateway_base_url", "https://localhost:5000/v1/api")
                or "https://localhost:5000/v1/api"
            ).rstrip("/"),
            verify_ssl=bool(getattr(settings, "ibkr_verify_ssl", False)),
            timeout_s=float(getattr(settings, "ibkr_timeout_s", 12.0) or 12.0),
            auth_backoff_s=float(getattr(settings, "ibkr_auth_backoff_s", 60.0) or 60.0),
            default_account_id=(
                str(getattr(settings, "ibkr_default_account_id", "") or "").strip() or None
            ),
            symbol_conids=_parse_symbol_conids(getattr(settings, "ibkr_symbol_conids_json", None)),
        )

    @property
    def active(self) -> bool:
        return bool(self.enabled or self.mock_enabled)


def _parse_symbol_conids(raw: object) -> dict[str, str]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return {
            str(k).strip().upper(): str(v).strip()
            for k, v in raw.items()
            if str(k).strip() and str(v).strip()
        }
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(k).strip().upper(): str(v).strip()
        for k, v in payload.items()
        if str(k).strip() and str(v).strip()
    }
