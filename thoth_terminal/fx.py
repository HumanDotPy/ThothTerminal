from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, date
import json
from threading import RLock
from typing import Any, Iterable
import weakref

from sqlalchemy.orm import Session

from thoth_terminal.config import settings
from thoth_terminal.db import utcnow
from thoth_terminal.models import FxRate
from thoth_terminal.net import fetch_with_fallback, FetchError


@dataclass(frozen=True)
class FxRatesSnapshot:
    base: str
    as_of: date
    fetched_at: datetime
    provider: str | None
    rates: dict[str, float]
    meta: dict[str, object]


_CACHE: dict[str, FxRatesSnapshot] = {}
_FX_TABLE_ENSURE_LOCK = RLock()
_FX_TABLE_ENSURED_ENGINES: "weakref.WeakSet[Any]" = weakref.WeakSet()


def _ensure_fx_table(session: Session) -> None:
    bind = session.get_bind()
    engine = getattr(bind, "engine", bind)
    if engine in _FX_TABLE_ENSURED_ENGINES:
        return
    with _FX_TABLE_ENSURE_LOCK:
        if engine in _FX_TABLE_ENSURED_ENGINES:
            return
        try:
            FxRate.__table__.create(bind=bind, checkfirst=True)
            _FX_TABLE_ENSURED_ENGINES.add(engine)
        except Exception:
            pass


def _fetch_json(url: str) -> dict:
    res = fetch_with_fallback(url, timeout_s=20.0)
    if not res.text:
        raise FetchError(f"Empty response for {url}", url=url)
    return json.loads(res.text)


def _fetch_frankfurter(base: str) -> FxRatesSnapshot:
    url = f"https://api.frankfurter.app/latest?from={base}"
    data = _fetch_json(url)
    rates = {k: float(v) for k, v in (data.get("rates") or {}).items()}
    rates[base] = 1.0
    as_of = data.get("date")
    as_of_date = date.fromisoformat(as_of) if as_of else utcnow().date()
    return FxRatesSnapshot(
        base=base,
        as_of=as_of_date,
        fetched_at=utcnow(),
        provider="frankfurter",
        rates=rates,
        meta={"date": as_of, "url": url},
    )


def _fetch_open_er_api(base: str) -> FxRatesSnapshot:
    url = f"https://open.er-api.com/v6/latest/{base}"
    data = _fetch_json(url)
    if str(data.get("result")).lower() not in {"success", "ok"}:
        raise FetchError(f"Bad FX response for {url}: {data.get('result')}", url=url)
    rates = {k: float(v) for k, v in (data.get("rates") or {}).items()}
    rates[base] = 1.0
    date_str = data.get("time_last_update_utc") or data.get("time_last_update")
    as_of_date = utcnow().date()
    if isinstance(date_str, str):
        try:
            as_of_date = datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S %z").date()
        except Exception:
            as_of_date = utcnow().date()
    return FxRatesSnapshot(
        base=base,
        as_of=as_of_date,
        fetched_at=utcnow(),
        provider="open_er_api",
        rates=rates,
        meta={"date": date_str, "url": url},
    )


def _fetch_rates(base: str, *, currencies: Iterable[str] | None = None) -> FxRatesSnapshot | None:
    base = base.upper().strip()
    primary = (settings.fx_primary_provider or "frankfurter").strip().lower()
    fallback = (settings.fx_fallback_provider or "").strip().lower() or None
    required = {c.upper() for c in (currencies or settings.fx_required_currencies or [])}

    primary_snapshot: FxRatesSnapshot | None = None
    try:
        if primary == "open_er_api":
            primary_snapshot = _fetch_open_er_api(base)
        else:
            primary_snapshot = _fetch_frankfurter(base)
    except Exception:
        primary_snapshot = None

    if not primary_snapshot:
        return None

    rates = dict(primary_snapshot.rates)
    meta = dict(primary_snapshot.meta)

    missing = {c for c in required if c and c != base and c not in rates}
    if missing and fallback:
        try:
            if fallback == "frankfurter":
                fallback_snapshot = _fetch_frankfurter(base)
            else:
                fallback_snapshot = _fetch_open_er_api(base)
            for cur in missing:
                if cur in fallback_snapshot.rates:
                    rates[cur] = fallback_snapshot.rates[cur]
            meta["fallback_provider"] = fallback_snapshot.provider
            meta["fallback_date"] = fallback_snapshot.meta.get("date")
        except Exception:
            pass

    return FxRatesSnapshot(
        base=base,
        as_of=primary_snapshot.as_of,
        fetched_at=utcnow(),
        provider=primary_snapshot.provider,
        rates=rates,
        meta=meta,
    )


def get_cached_rates(base: str) -> FxRatesSnapshot | None:
    return _CACHE.get(base.upper())


def get_latest_rates(session: Session, base: str) -> FxRatesSnapshot | None:
    base = base.upper().strip()
    cached = _CACHE.get(base)
    if cached:
        return cached
    latest = (
        session.query(FxRate)
        .filter_by(base=base)
        .order_by(FxRate.as_of_date.desc(), FxRate.fetched_at.desc())
        .first()
    )
    if not latest:
        return None
    snapshot = FxRatesSnapshot(
        base=latest.base,
        as_of=latest.as_of_date,
        fetched_at=latest.fetched_at,
        provider=latest.provider,
        rates=dict(latest.rates or {}),
        meta={},
    )
    _CACHE[base] = snapshot
    return snapshot


def ensure_fx_rates(session: Session, *, base: str | None = None) -> FxRatesSnapshot | None:
    if not getattr(settings, "fx_enabled", True):
        return None
    base = (base or settings.base_currency or "USD").upper().strip()
    now = utcnow()
    max_age = int(getattr(settings, "fx_update_max_age_hours", 36) or 36)

    try:
        _ensure_fx_table(session)
    except Exception:
        pass

    latest = get_latest_rates(session, base)
    if latest:
        age = now - latest.fetched_at
        if latest.as_of >= now.date() or age < timedelta(hours=max_age):
            return latest

    snapshot = _fetch_rates(base, currencies=settings.fx_required_currencies)
    if not snapshot:
        return latest

    existing = (
        session.query(FxRate)
        .filter_by(base=base, as_of_date=snapshot.as_of)
        .order_by(FxRate.fetched_at.desc())
        .first()
    )
    if existing:
        existing.rates = snapshot.rates
        existing.provider = snapshot.provider
        existing.fetched_at = snapshot.fetched_at
        session.add(existing)
    else:
        session.add(
            FxRate(
                base=base,
                as_of_date=snapshot.as_of,
                fetched_at=snapshot.fetched_at,
                provider=snapshot.provider,
                rates=snapshot.rates,
            )
        )

    _CACHE[base] = snapshot
    return snapshot


def convert_to_base(amount: float, currency: str, rates: FxRatesSnapshot) -> float | None:
    cur = currency.upper().strip()
    if not cur:
        return None
    if cur == rates.base:
        return float(amount)
    rate = rates.rates.get(cur)
    if not rate:
        return None
    return float(amount) / float(rate)
