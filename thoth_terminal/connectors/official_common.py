from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from thoth_terminal.connectors.web_list import _matches_keywords


def keyword_allowed(config: dict[str, Any] | None, *parts: object) -> bool:
    cfg = dict(config or {})
    include = [str(v).strip() for v in (cfg.get("include_keywords") or []) if str(v).strip()]
    exclude = [str(v).strip() for v in (cfg.get("exclude_keywords") or []) if str(v).strip()]
    text = " ".join(str(part or "").strip() for part in parts if str(part or "").strip())
    return _matches_keywords(text, include=include, exclude=exclude)


def stable_external_id(prefix: str, *parts: object) -> str:
    payload = "|".join(str(part or "").strip() for part in parts if str(part or "").strip())
    digest = hashlib.sha1(payload.encode("utf-8", errors="ignore")).hexdigest()[:20]
    return f"{prefix}:{digest}"


def parse_datetime_candidates(value: object, fmts: list[str]) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in fmts:
        try:
            return datetime.strptime(raw, fmt)
        except Exception:
            continue
    return None


def recent_date_window(config: dict[str, Any] | None, *, default_days: int = 1) -> tuple[datetime, datetime]:
    cfg = dict(config or {})
    now = datetime.now()
    start_raw = str(cfg.get("start_date") or "").strip()
    end_raw = str(cfg.get("end_date") or "").strip()
    start_dt = parse_datetime_candidates(start_raw, ["%Y-%m-%d", "%Y%m%d"])
    end_dt = parse_datetime_candidates(end_raw, ["%Y-%m-%d", "%Y%m%d"])
    if end_dt is None:
        end_dt = now
    else:
        end_dt = end_dt.replace(hour=23, minute=59, second=59)
    if start_dt is None:
        start_dt = (end_dt - timedelta(days=max(0, int(cfg.get("lookback_days") or default_days)))).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    else:
        start_dt = start_dt.replace(hour=0, minute=0, second=0)
    return start_dt, end_dt


def safe_json_dumps(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(value or "")
