from __future__ import annotations

from datetime import datetime, timedelta

from thoth_terminal.config import settings


BROWSER_CONNECTORS = frozenset(
    {
        "web_list_browser",
        "x_list",
        "substack_inbox_browser",
        "note_creator_browser",
    }
)


def is_browser_connector(config: dict[str, object] | None) -> bool:
    cfg = config or {}
    connector = str(cfg.get("connector") or "").strip().lower()
    return connector in BROWSER_CONNECTORS or (connector == "web_list" and bool(cfg.get("use_browser")))


def browser_circuit_backoff_seconds(config: dict[str, object] | None, failures: int) -> int | None:
    if not is_browser_connector(config):
        return None
    threshold = max(
        1,
        int(
            (config or {}).get("browser_failure_quarantine_threshold")
            or getattr(settings, "browser_failure_quarantine_threshold", 8)
        ),
    )
    failure_count = max(0, int(failures or 0))
    if failure_count < threshold:
        return None
    base = max(
        3600,
        int(
            (config or {}).get("browser_failure_quarantine_seconds")
            or getattr(settings, "browser_failure_quarantine_seconds", 86400)
        ),
    )
    maximum = max(
        base,
        int(
            (config or {}).get("browser_failure_quarantine_max_seconds")
            or getattr(settings, "browser_failure_quarantine_max_seconds", 604800)
        ),
    )
    exponent = min(8, failure_count - threshold)
    return min(maximum, base * (2**exponent))


def browser_circuit_retry_at(
    config: dict[str, object] | None,
    *,
    failures: int,
    last_run_at: datetime | None,
    backoff_until: datetime | None,
    now: datetime,
) -> datetime | None:
    seconds = browser_circuit_backoff_seconds(config, failures)
    if seconds is None:
        return None
    candidates = [value for value in (backoff_until,) if value is not None and value > now]
    if last_run_at is not None:
        derived = last_run_at + timedelta(seconds=seconds)
        if derived > now:
            candidates.append(derived)
    if candidates:
        return max(candidates)
    # The circuit has served its full quarantine. Permit one probe; another
    # failure will reopen it from the new last_run_at.
    return None
