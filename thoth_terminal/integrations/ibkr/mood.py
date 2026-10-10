from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from thoth_terminal.integrations.ibkr.models import MoodResult, PortfolioMetrics


MOOD_STATES = {
    "neutral",
    "happy",
    "ecstatic",
    "manic",
    "sad",
    "depressed",
    "miserable",
    "angry",
    "enraged",
    "afraid",
    "stressed",
    "terrified",
}

_CRITICAL_MOODS = {"manic", "miserable", "enraged", "terrified"}


def classify_portfolio_mood(metrics: PortfolioMetrics) -> MoodResult:
    daily_pct = _num(metrics.daily_pnl_pct)
    daily_pnl = _num(metrics.daily_pnl)
    ytd_pct = _num(metrics.ytd_pnl_pct)
    mtd_pct = _num(metrics.mtd_pnl_pct)
    gross_pct = _num(metrics.gross_exposure_pct)
    largest_weight = _num(metrics.largest_position_weight)
    reversal = _num(metrics.intraday_reversal_pct)
    volatility = _num(metrics.volatility_proxy)
    detractor_pnl = _num(metrics.largest_detractor_pnl)

    primary = "neutral"
    secondary = None
    intensity = 0
    reasons: list[str] = []

    if daily_pct <= -5.0:
        primary, intensity = "miserable", 3
        reasons.append("Daily P&L is severely negative.")
    elif daily_pct <= -2.0:
        primary, intensity = "depressed", 2
        reasons.append("Daily P&L is materially negative.")
    elif daily_pct <= -0.5:
        primary, intensity = "sad", 1
        reasons.append("Daily P&L is negative.")
    elif daily_pct >= 5.0:
        primary, intensity = "ecstatic", 3
        reasons.append("Daily P&L is extremely positive.")
    elif daily_pct >= 2.0:
        primary, intensity = "happy", 2
        reasons.append("Daily P&L is strongly positive.")
    elif daily_pct >= 0.35:
        primary, intensity = "happy", 1
        reasons.append("Daily P&L is positive.")
    else:
        reasons.append("Daily P&L is close to flat.")

    concentration_high = largest_weight >= 0.35
    exposure_high = gross_pct >= 1.50
    exposure_extreme = gross_pct >= 2.25
    reversal_bad = reversal <= -1.5
    volatile = volatility >= 2.0

    if daily_pct > 1.0 and (concentration_high or exposure_high):
        primary = "manic"
        intensity = max(intensity, 2 if not exposure_extreme else 3)
        if concentration_high:
            reasons.append("The positive move is concentrated in a large position.")
        if exposure_high:
            reasons.append("Gross exposure is elevated while the book is up.")
    elif exposure_extreme and daily_pct > -0.5:
        primary = "stressed"
        intensity = max(intensity, 2)
        reasons.append("Gross exposure is extreme even without a large loss.")
    elif exposure_high and primary in {"neutral", "happy"}:
        secondary = "stressed"
        reasons.append("Gross exposure is elevated.")

    if daily_pct < -0.25 and detractor_pnl < 0 and abs(detractor_pnl) >= max(1000.0, abs(daily_pnl) * 0.55):
        primary = "enraged" if daily_pct <= -3.0 else "angry"
        intensity = max(intensity, 3 if primary == "enraged" else 2)
        reasons.append("One position explains most of the loss.")

    if reversal_bad:
        if primary in {"happy", "ecstatic"}:
            primary = "stressed"
            intensity = max(intensity, 2)
        else:
            secondary = secondary or "stressed"
            intensity = max(intensity, 1)
        reasons.append("The book has reversed sharply intraday.")

    if volatile:
        if daily_pct <= -2.0 or exposure_high:
            primary = "terrified" if volatility >= 3.5 else "afraid"
            intensity = max(intensity, 3 if primary == "terrified" else 2)
        elif secondary is None:
            secondary = "afraid"
        reasons.append("Volatility or uncertainty is elevated.")

    if ytd_pct <= -15.0 and primary in {"neutral", "sad"}:
        secondary = secondary or "depressed"
        reasons.append("YTD performance is weighing on the background state.")
    elif ytd_pct >= 25.0 and primary in {"happy", "ecstatic"}:
        reasons.append("YTD performance reinforces the positive state.")

    if mtd_pct <= -8.0 and primary in {"neutral", "sad"}:
        secondary = secondary or "stressed"
        reasons.append("MTD performance remains weak.")

    primary = primary if primary in MOOD_STATES else "neutral"
    intensity = max(0, min(3, int(intensity)))
    caption = _caption(primary, intensity)
    return MoodResult(
        account_id=metrics.account_id,
        primary_mood=primary,
        secondary_mood=secondary,
        intensity=intensity,
        caption=caption,
        reasons=_dedupe(reasons)[:6],
        metrics={
            "net_liquidation": metrics.net_liquidation,
            "cash": metrics.cash,
            "gross_exposure": metrics.gross_exposure,
            "net_exposure": metrics.net_exposure,
            "daily_pnl_pct": metrics.daily_pnl_pct,
            "daily_pnl": metrics.daily_pnl,
            "unrealized_pnl": metrics.unrealized_pnl,
            "realized_pnl": metrics.realized_pnl,
            "mtd_pnl_pct": metrics.mtd_pnl_pct,
            "ytd_pnl_pct": metrics.ytd_pnl_pct,
            "gross_exposure_pct": metrics.gross_exposure_pct,
            "cash_pct": metrics.cash_pct,
            "largest_position_weight": metrics.largest_position_weight,
            "largest_contributor": metrics.largest_contributor,
            "largest_detractor": metrics.largest_detractor,
            "intraday_reversal_pct": metrics.intraday_reversal_pct,
            "volatility_proxy": metrics.volatility_proxy,
        },
        sprite_asset_key=f"{primary}:{intensity}",
    )


def smooth_mood(
    current: MoodResult,
    previous: MoodResult | None,
    *,
    previous_at: datetime | None,
    now: datetime,
    ordinary_min_duration_s: int = 120,
) -> MoodResult:
    if previous is None or previous_at is None:
        return current
    if current.primary_mood == previous.primary_mood and current.intensity == previous.intensity:
        return current
    if current.primary_mood in _CRITICAL_MOODS or current.intensity >= 3:
        return current
    if now - previous_at < timedelta(seconds=max(0, int(ordinary_min_duration_s))):
        reasons = list(current.reasons)
        reasons.insert(0, "Mood smoothing is holding the previous state briefly.")
        return MoodResult(
            account_id=current.account_id,
            primary_mood=previous.primary_mood,
            secondary_mood=current.secondary_mood or previous.secondary_mood,
            intensity=previous.intensity,
            caption=previous.caption,
            reasons=_dedupe(reasons)[:6],
            metrics=current.metrics,
            sprite_asset_key=previous.sprite_asset_key,
        )
    return current


def _caption(mood: str, intensity: int) -> str:
    captions = {
        "neutral": "The portfolio is quiet.",
        "happy": "The portfolio is pleased.",
        "ecstatic": "The portfolio is euphoric.",
        "manic": "The portfolio is euphoric, but unstable.",
        "sad": "The portfolio is bruised.",
        "depressed": "The portfolio is heavy.",
        "miserable": "The portfolio is in pain.",
        "angry": "The portfolio is angry at a concentrated loss.",
        "enraged": "The portfolio is furious about a severe hit.",
        "afraid": "The portfolio is nervous about risk.",
        "stressed": "The portfolio is tense.",
        "terrified": "The portfolio is terrified by risk and losses.",
    }
    if mood == "neutral" and intensity == 0:
        return captions[mood]
    return captions.get(mood, "The portfolio state is unclear.")


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        clean = str(item or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out
