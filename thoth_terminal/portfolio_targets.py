from __future__ import annotations

import json
import math
import os
from datetime import date
from pathlib import Path
from typing import Any

from thoth_terminal.config import settings
from thoth_terminal.db import utcnow


DEFAULT_PORTFOLIO_TARGETS: list[dict[str, Any]] = [
    {"id": "2026-q2", "label": "Q2 2026", "due": "2026-06-30", "value": 50000.0},
    {"id": "2026-q3", "label": "Q3 2026", "due": "2026-09-30", "value": 60000.0},
    {"id": "2026-eoy-blue-sky", "label": "EOY 2026 Blue Sky", "due": "2026-12-31", "value": 70000.0},
    {"id": "2027-q2", "label": "Q2 2027", "due": "2027-06-30", "value": 100000.0},
]


def portfolio_targets_path(settings_obj: object | None = None) -> Path:
    cfg = settings_obj or settings
    return Path(getattr(cfg, "runtime_state_dir", settings.runtime_state_dir)) / "portfolio_targets.json"


def read_portfolio_target_profile(
    *,
    settings_obj: object | None = None,
) -> dict[str, Any]:
    path = portfolio_targets_path(settings_obj)
    raw: dict[str, Any] = {}
    try:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            raw = payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        raw = {}
    return normalize_portfolio_target_profile(raw)


def write_portfolio_target_profile(
    payload: dict[str, Any],
    *,
    settings_obj: object | None = None,
) -> dict[str, Any]:
    profile = normalize_portfolio_target_profile(payload)
    profile["updated_at"] = utcnow().isoformat()
    path = portfolio_targets_path(settings_obj)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_path, path)
    return profile


def normalize_portfolio_target_profile(payload: dict[str, Any] | None) -> dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    targets = raw.get("targets")
    normalized_targets = normalize_portfolio_targets(targets if isinstance(targets, list) else None)
    basis = normalize_portfolio_basis(raw.get("basis") if isinstance(raw.get("basis"), dict) else {})
    updated_at = str(raw.get("updated_at") or "").strip() or None
    return {
        "targets": normalized_targets,
        "basis": basis,
        "updated_at": updated_at,
    }


def normalize_portfolio_targets(rows: list[Any] | None) -> list[dict[str, Any]]:
    source = rows if rows else DEFAULT_PORTFOLIO_TARGETS
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(source):
        if not isinstance(row, dict):
            continue
        fallback = DEFAULT_PORTFOLIO_TARGETS[min(index, len(DEFAULT_PORTFOLIO_TARGETS) - 1)]
        value = _positive_float(row.get("value"), fallback["value"])
        if value is None:
            continue
        due = _valid_due_date(row.get("due")) or str(fallback["due"])
        normalized.append(
            {
                "id": _target_id(row.get("id"), fallback["id"], index),
                "label": _label(row.get("label"), fallback["label"], index),
                "due": due,
                "value": value,
            }
        )
    return sorted(normalized or [dict(row) for row in DEFAULT_PORTFOLIO_TARGETS], key=lambda row: row["due"])


def normalize_portfolio_basis(raw: dict[str, Any]) -> dict[str, Any]:
    starting_equity = _optional_float(raw.get("starting_equity"))
    net_contributions = _optional_float(raw.get("net_contributions"))
    as_of = _valid_due_date(raw.get("as_of")) if raw.get("as_of") else None
    return {
        "starting_equity": starting_equity,
        "net_contributions": net_contributions,
        "as_of": as_of,
        "mode": "manual",
    }


def _target_id(value: Any, fallback: str, index: int) -> str:
    raw = str(value or fallback or f"target-{index + 1}").strip().lower()
    keep = [ch if ch.isalnum() else "-" for ch in raw]
    compact = "-".join(part for part in "".join(keep).split("-") if part)
    return compact[:64] or f"target-{index + 1}"


def _label(value: Any, fallback: str, index: int) -> str:
    raw = str(value or fallback or f"Target {index + 1}").strip()
    return raw[:80] or f"Target {index + 1}"


def _valid_due_date(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = date.fromisoformat(raw[:10])
    except ValueError:
        return None
    return parsed.isoformat()


def _positive_float(value: Any, default: Any = None) -> float | None:
    parsed = _optional_float(value)
    if parsed is None or parsed <= 0:
        parsed = _optional_float(default)
    return parsed if parsed is not None and parsed > 0 else None


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed
