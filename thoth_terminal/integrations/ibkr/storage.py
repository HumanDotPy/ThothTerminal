from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.integrations.ibkr.models import MoodResult, PortfolioMetrics, PositionMetrics
from thoth_terminal.models import (
    IbkrRequestLog,
    PortfolioMoodSnapshot,
    PortfolioSnapshot,
    PositionSnapshot,
)


def log_ibkr_request(
    session: Session,
    *,
    endpoint: str,
    method: str,
    status_code: int | None,
    duration_ms: float | None,
    cache_hit: bool,
    error_summary: str | None = None,
) -> IbkrRequestLog:
    row = IbkrRequestLog(
        endpoint=str(endpoint)[:255],
        method=str(method or "GET").upper()[:8],
        requested_at=utcnow(),
        status_code=status_code,
        duration_ms=duration_ms,
        cache_hit=bool(cache_hit),
        error_summary=str(error_summary)[:1000] if error_summary else None,
    )
    session.add(row)
    return row


def store_portfolio_snapshot(
    session: Session,
    *,
    metrics: PortfolioMetrics,
    positions: list[PositionMetrics],
    captured_at: datetime | None = None,
) -> PortfolioSnapshot:
    captured = captured_at or utcnow()
    row = PortfolioSnapshot(
        account_id=metrics.account_id,
        captured_at=captured,
        net_liquidation=metrics.net_liquidation,
        cash=metrics.cash,
        gross_exposure=metrics.gross_exposure,
        net_exposure=metrics.net_exposure,
        daily_pnl=metrics.daily_pnl,
        daily_pnl_pct=metrics.daily_pnl_pct,
        unrealized_pnl=metrics.unrealized_pnl,
        realized_pnl=metrics.realized_pnl,
        mtd_pnl_pct=metrics.mtd_pnl_pct,
        ytd_pnl_pct=metrics.ytd_pnl_pct,
        metadata_json=metrics.metadata,
    )
    session.add(row)
    session.flush()
    for pos in positions:
        session.add(
            PositionSnapshot(
                portfolio_snapshot_id=int(row.id),
                account_id=pos.account_id,
                symbol=pos.symbol,
                conid=pos.conid,
                asset_class=pos.asset_class,
                quantity=pos.quantity,
                market_value=pos.market_value,
                average_cost=pos.average_cost,
                unrealized_pnl=pos.unrealized_pnl,
                realized_pnl=pos.realized_pnl,
                weight=pos.weight,
                metadata_json=pos.metadata,
            )
        )
    return row


def latest_portfolio_snapshot(session: Session, *, account_id: str | None = None) -> PortfolioSnapshot | None:
    query = session.query(PortfolioSnapshot)
    if account_id:
        query = query.filter(PortfolioSnapshot.account_id == account_id)
    return query.order_by(PortfolioSnapshot.captured_at.desc(), PortfolioSnapshot.id.desc()).first()


def latest_position_snapshots(session: Session, snapshot_id: int) -> list[PositionSnapshot]:
    return (
        session.query(PositionSnapshot)
        .filter(PositionSnapshot.portfolio_snapshot_id == int(snapshot_id))
        .order_by(PositionSnapshot.market_value.desc().nullslast(), PositionSnapshot.id.asc())
        .all()
    )


def store_mood_snapshot(
    session: Session,
    *,
    mood: MoodResult,
    captured_at: datetime | None = None,
) -> PortfolioMoodSnapshot:
    row = PortfolioMoodSnapshot(
        account_id=mood.account_id,
        captured_at=captured_at or utcnow(),
        primary_mood=mood.primary_mood,
        secondary_mood=mood.secondary_mood,
        intensity=mood.intensity,
        caption=mood.caption,
        sprite_asset_key=mood.sprite_asset_key,
        reasons_json=mood.reasons,
        metrics_json=mood.metrics,
    )
    session.add(row)
    return row


def latest_mood_snapshot(session: Session, *, account_id: str | None = None) -> PortfolioMoodSnapshot | None:
    query = session.query(PortfolioMoodSnapshot)
    if account_id:
        query = query.filter(PortfolioMoodSnapshot.account_id == account_id)
    return query.order_by(
        PortfolioMoodSnapshot.captured_at.desc(),
        PortfolioMoodSnapshot.id.desc(),
    ).first()


def portfolio_metrics_from_snapshot(row: PortfolioSnapshot, positions: list[PositionSnapshot]) -> PortfolioMetrics:
    largest = max((p.weight or 0.0 for p in positions), default=0.0)
    gross_pct = None
    cash_pct = None
    if row.net_liquidation:
        gross_pct = (row.gross_exposure or 0.0) / row.net_liquidation
        cash_pct = (row.cash or 0.0) / row.net_liquidation
    contributor = _extreme_symbol(positions, positive=True)
    detractor = _extreme_symbol(positions, positive=False)
    detractor_pnl = None
    if detractor:
        for pos in positions:
            if pos.symbol == detractor:
                detractor_pnl = pos.unrealized_pnl
                break
    return PortfolioMetrics(
        account_id=row.account_id,
        net_liquidation=row.net_liquidation,
        cash=row.cash,
        gross_exposure=row.gross_exposure,
        net_exposure=row.net_exposure,
        daily_pnl=row.daily_pnl,
        daily_pnl_pct=row.daily_pnl_pct,
        unrealized_pnl=row.unrealized_pnl,
        realized_pnl=row.realized_pnl,
        mtd_pnl_pct=row.mtd_pnl_pct,
        ytd_pnl_pct=row.ytd_pnl_pct,
        cash_pct=cash_pct,
        gross_exposure_pct=gross_pct,
        largest_position_weight=largest,
        largest_contributor=contributor,
        largest_detractor=detractor,
        largest_detractor_pnl=detractor_pnl,
        metadata=dict(row.metadata_json or {}),
    )


def _extreme_symbol(positions: list[PositionSnapshot], *, positive: bool) -> str | None:
    best_symbol = None
    best_value = 0.0
    for pos in positions:
        value = float(pos.unrealized_pnl or 0.0)
        if positive and value > best_value:
            best_value = value
            best_symbol = pos.symbol
        if (not positive) and value < best_value:
            best_value = value
            best_symbol = pos.symbol
    return best_symbol


def compact_snapshot_payload(row: PortfolioSnapshot, positions: list[PositionSnapshot]) -> dict[str, Any]:
    return {
        "id": row.id,
        "account_id": row.account_id,
        "captured_at": row.captured_at.isoformat() if row.captured_at else None,
        "net_liquidation": row.net_liquidation,
        "cash": row.cash,
        "gross_exposure": row.gross_exposure,
        "net_exposure": row.net_exposure,
        "daily_pnl": row.daily_pnl,
        "daily_pnl_pct": row.daily_pnl_pct,
        "unrealized_pnl": row.unrealized_pnl,
        "realized_pnl": row.realized_pnl,
        "mtd_pnl_pct": row.mtd_pnl_pct,
        "ytd_pnl_pct": row.ytd_pnl_pct,
        "positions": [
            {
                "symbol": pos.symbol,
                "conid": pos.conid,
                "asset_class": pos.asset_class,
                "quantity": pos.quantity,
                "market_value": pos.market_value,
                "average_cost": pos.average_cost,
                "unrealized_pnl": pos.unrealized_pnl,
                "realized_pnl": pos.realized_pnl,
                "weight": pos.weight,
                "metadata": dict(pos.metadata_json or {}),
            }
            for pos in positions[:50]
        ],
    }
