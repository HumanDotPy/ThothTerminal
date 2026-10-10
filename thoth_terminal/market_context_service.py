from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.config import settings
from thoth_terminal.db import utcnow
from thoth_terminal.fx import get_latest_rates
from thoth_terminal.home_intelligence import build_market_watchlist
from thoth_terminal.integrations.ibkr.routes import ibkr_service
from thoth_terminal.integrations.ibkr.storage import (
    compact_snapshot_payload,
    latest_portfolio_snapshot,
    latest_position_snapshots,
)
from thoth_terminal.models import PortfolioMoodSnapshot, ResearchInstrument


_INDEX_TYPES = {"IND", "INDEX", "IDX"}
_FUTURE_TYPES = {"FUT", "FUTURE", "CONT_FUT", "CONTFUT"}
_COMMODITY_TYPES = {"CMDTY", "COMMODITY", "CASHCOMMODITY"}


def _instrument_payload(row: ResearchInstrument) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "instrument_key": row.instrument_key,
        "display_name": row.display_name,
        "ticker": row.ticker,
        "exchange": row.exchange,
        "currency": row.currency,
        "security_type": row.security_type,
        "ibkr_conid": row.ibkr_conid,
        "yahoo_symbol": row.yahoo_symbol,
        "provider": row.primary_provider,
        "metadata": dict(row.metadata_json or {}),
    }


def build_agent_market_context(
    session: Session,
    *,
    base_currency: str | None = None,
    live_quotes: bool = False,
    max_items: int = 30,
) -> dict[str, Any]:
    base = str(base_currency or settings.base_currency or "USD").strip().upper() or "USD"
    fx_snapshot = get_latest_rates(session, base=base)
    fx_payload = {
        "base": base,
        "as_of": fx_snapshot.as_of.isoformat() if fx_snapshot else None,
        "provider": fx_snapshot.provider if fx_snapshot else None,
        "rates": dict(fx_snapshot.rates or {}) if fx_snapshot else {},
    }
    ibkr_status = ibkr_service().status(live=False)
    latest_portfolio = latest_portfolio_snapshot(session)
    portfolio_payload = None
    if latest_portfolio is not None:
        portfolio_payload = compact_snapshot_payload(
            latest_portfolio,
            latest_position_snapshots(session, int(latest_portfolio.id)),
        )
    mood = (
        session.query(PortfolioMoodSnapshot)
        .order_by(PortfolioMoodSnapshot.captured_at.desc(), PortfolioMoodSnapshot.id.desc())
        .first()
    )
    instruments = (
        session.query(ResearchInstrument)
        .filter(ResearchInstrument.security_type.in_(sorted(_INDEX_TYPES | _FUTURE_TYPES | _COMMODITY_TYPES)))
        .order_by(ResearchInstrument.updated_at.desc(), ResearchInstrument.id.desc())
        .limit(max(1, min(int(max_items or 30), 100)))
        .all()
    )
    indexes = [_instrument_payload(row) for row in instruments if str(row.security_type or "").upper() in _INDEX_TYPES]
    futures = [_instrument_payload(row) for row in instruments if str(row.security_type or "").upper() in _FUTURE_TYPES]
    commodities = [_instrument_payload(row) for row in instruments if str(row.security_type or "").upper() in _COMMODITY_TYPES]
    return {
        "generated_at": utcnow().isoformat(),
        "fx": fx_payload,
        "market_watchlist": build_market_watchlist(session, allow_live_refresh=bool(live_quotes)),
        "indexes": indexes,
        "futures": futures,
        "commodities": commodities,
        "ibkr": {
            "enabled": bool(ibkr_status.get("enabled")),
            "connected": bool(ibkr_status.get("connected")),
            "auth_required": bool(ibkr_status.get("auth_required")),
            "market_data": "available" if ibkr_status.get("connected") else "offline_or_delayed",
        },
        "portfolio": portfolio_payload,
        "portfolio_mood": {
            "primary_mood": mood.primary_mood,
            "secondary_mood": mood.secondary_mood,
            "intensity": int(mood.intensity or 0),
            "captured_at": mood.captured_at.isoformat() if mood.captured_at else None,
            "metrics": dict(mood.metrics_json or {}),
        }
        if mood is not None
        else None,
        "policy": "Universe-first context; portfolio positions are included as context only and do not determine research priority.",
    }
