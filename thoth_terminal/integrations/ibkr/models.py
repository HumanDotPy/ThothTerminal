from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PortfolioMetrics:
    account_id: str = "mock"
    net_liquidation: float | None = None
    cash: float | None = None
    gross_exposure: float | None = None
    net_exposure: float | None = None
    daily_pnl: float | None = None
    daily_pnl_pct: float | None = None
    unrealized_pnl: float | None = None
    realized_pnl: float | None = None
    mtd_pnl_pct: float | None = None
    ytd_pnl_pct: float | None = None
    cash_pct: float | None = None
    gross_exposure_pct: float | None = None
    largest_position_weight: float | None = None
    largest_contributor: str | None = None
    largest_detractor: str | None = None
    largest_detractor_pnl: float | None = None
    intraday_reversal_pct: float | None = None
    volatility_proxy: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PositionMetrics:
    account_id: str
    symbol: str | None = None
    conid: str | None = None
    asset_class: str | None = None
    quantity: float | None = None
    market_value: float | None = None
    average_cost: float | None = None
    unrealized_pnl: float | None = None
    realized_pnl: float | None = None
    weight: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MoodResult:
    account_id: str
    primary_mood: str
    secondary_mood: str | None
    intensity: int
    caption: str
    reasons: list[str]
    metrics: dict[str, Any]
    sprite_asset_key: str
