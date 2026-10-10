from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any


def _settings_or_default(settings_obj: object | None = None) -> object:
    if settings_obj is not None:
        return settings_obj
    from thoth_terminal.config import settings

    return settings


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _parse_expiry_date(value: object) -> tuple[date | None, str | None]:
    text = str(value or "").strip()
    if not text:
        return None, None
    try:
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            return date.fromisoformat(text[:10]), None
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.date(), None
    except Exception:
        return None, f"Invalid expiry date {text!r}; expected YYYY-MM-DD or ISO timestamp."


def _credential_status(
    *,
    name: str,
    label: str,
    key_env: str,
    configured: bool,
    expires_at: object,
    warning_days: int,
    today: date,
) -> dict[str, object]:
    expires_on, parse_error = _parse_expiry_date(expires_at)
    if not configured:
        return {
            "name": name,
            "label": label,
            "key_env": key_env,
            "configured": False,
            "status": "missing",
            "ok": True,
            "expires_at": str(expires_at or "") or None,
            "days_until_expiry": None,
            "message": f"{key_env} is not configured.",
        }
    if parse_error:
        return {
            "name": name,
            "label": label,
            "key_env": key_env,
            "configured": True,
            "status": "invalid_expiry",
            "ok": False,
            "expires_at": str(expires_at or "") or None,
            "days_until_expiry": None,
            "message": parse_error,
        }
    if expires_on is None:
        return {
            "name": name,
            "label": label,
            "key_env": key_env,
            "configured": True,
            "status": "unknown_expiry",
            "ok": True,
            "expires_at": None,
            "days_until_expiry": None,
            "message": f"{key_env} is configured, but no expiry date is recorded.",
        }

    days_left = (expires_on - today).days
    if days_left < 0:
        status = "expired"
        ok = False
        message = f"{key_env} expired on {expires_on.isoformat()}."
    elif days_left <= max(0, warning_days):
        status = "expiring_soon"
        ok = True
        message = f"{key_env} expires on {expires_on.isoformat()} ({days_left} days left)."
    else:
        status = "valid"
        ok = True
        message = f"{key_env} expires on {expires_on.isoformat()} ({days_left} days left)."

    return {
        "name": name,
        "label": label,
        "key_env": key_env,
        "configured": True,
        "status": status,
        "ok": ok,
        "expires_at": expires_on.isoformat(),
        "days_until_expiry": days_left,
        "message": message,
    }


def official_api_key_health(
    *,
    settings_obj: object | None = None,
    today: date | None = None,
) -> dict[str, object]:
    settings = _settings_or_default(settings_obj)
    warning_days = int(getattr(settings, "api_key_expiry_warning_days", 14) or 14)
    today_val = today or _today_utc()
    statuses = [
        _credential_status(
            name="edinet",
            label="EDINET",
            key_env="THOTH_EDINET_SUBSCRIPTION_KEY",
            configured=bool(getattr(settings, "edinet_subscription_key", None)),
            expires_at=getattr(settings, "edinet_subscription_key_expires_at", None),
            warning_days=warning_days,
            today=today_val,
        ),
        _credential_status(
            name="usitc_dataweb",
            label="USITC DataWeb",
            key_env="THOTH_USITC_DATAWEB_API_KEY",
            configured=bool(getattr(settings, "usitc_dataweb_api_key", None)),
            expires_at=getattr(settings, "usitc_dataweb_api_key_expires_at", None),
            warning_days=warning_days,
            today=today_val,
        ),
        _credential_status(
            name="un_comtrade",
            label="UN Comtrade",
            key_env="THOTH_UN_COMTRADE_API_KEY",
            configured=bool(getattr(settings, "un_comtrade_api_key", None)),
            expires_at=getattr(settings, "un_comtrade_api_key_expires_at", None),
            warning_days=warning_days,
            today=today_val,
        ),
        _credential_status(
            name="sam_gov",
            label="SAM.gov",
            key_env="THOTH_SAM_GOV_API_KEY",
            configured=bool(getattr(settings, "sam_gov_api_key", None)),
            expires_at=getattr(settings, "sam_gov_api_key_expires_at", None),
            warning_days=warning_days,
            today=today_val,
        ),
    ]
    by_key = {str(row["name"]): row for row in statuses}
    blocking = [
        row
        for row in statuses
        if row.get("status") in {"expired", "invalid_expiry"}
    ]
    warnings = [
        row
        for row in statuses
        if row.get("status") in {"expiring_soon", "unknown_expiry"}
    ]
    return {
        "ok": not blocking,
        "warning_days": warning_days,
        "statuses": statuses,
        "by_key": by_key,
        "blocking": blocking,
        "warnings": warnings,
    }


def official_api_key_status(
    name: str,
    *,
    settings_obj: object | None = None,
    today: date | None = None,
) -> dict[str, object] | None:
    health = official_api_key_health(settings_obj=settings_obj, today=today)
    by_key = health.get("by_key") if isinstance(health, dict) else {}
    if not isinstance(by_key, dict):
        return None
    row = by_key.get(str(name))
    return row if isinstance(row, dict) else None
