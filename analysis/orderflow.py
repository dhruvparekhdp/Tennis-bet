"""
Order flow and Cumulative Volume Delta (CVD) analytics.

Measures the aggression of market participants by comparing taker buy volume
against taker sell volume. Identifies passive limit absorption at key levels
and detects whether aggressive market orders confirm or contradict price action.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from analysis.crypto_state import OHLCVCandle


def bar_delta(candle: OHLCVCandle) -> float:
    """
    Net aggression for a single bar.

    Positive = buyers crossed the spread (taker buys).
    Negative = sellers hit the bids (taker sells).
    """
    if candle.taker_buy_volume <= 0:
        return 0.0
    taker_sell = max(0.0, candle.volume - candle.taker_buy_volume)
    return candle.taker_buy_volume - taker_sell


def cvd_series(candles: list[OHLCVCandle]) -> list[float]:
    """
    Cumulative Volume Delta across a series of closed bars.
    """
    out: list[float] = []
    total = 0.0
    for c in candles:
        if not c.is_closed:
            continue
        total += bar_delta(c)
        out.append(total)
    return out


def cvd_delta_window(candles: list[OHLCVCandle], window: int = 15) -> float:
    """
    Net volume delta over the last `window` closed bars.
    """
    closed = [c for c in candles if c.is_closed]
    if not closed:
        return 0.0
    recent = closed[-window:]
    return sum(bar_delta(c) for c in recent)


def detect_absorption(candles: list[OHLCVCandle], lookback: int = 20) -> str | None:
    """
    Detects institutional absorption at swing highs/lows.

    - Bullish Absorption: Price dipped to new local lows, but delta was strongly
      positive (aggressive sellers were absorbed by massive limit buy walls, or
      aggressive buyers stepped in immediately).
    - Bearish Absorption: Price pushed to new local highs, but delta was strongly
      negative (aggressive buyers were absorbed by limit sell walls).
    """
    closed = [c for c in candles if c.is_closed]
    if len(closed) < lookback:
        return None

    recent = closed[-lookback:]
    deltas = [bar_delta(c) for c in recent]
    has_delta = any(abs(d) > 0 for d in deltas)
    if not has_delta:
        return None

    last_bars = recent[-3:]
    last_deltas = deltas[-3:]
    prior_bars = recent[:-3]

    min_prior_low = min(c.low for c in prior_bars)
    max_prior_high = max(c.high for c in prior_bars)

    recent_low = min(c.low for c in last_bars)
    recent_high = max(c.high for c in last_bars)
    net_last_delta = sum(last_deltas)

    # Bullish Absorption: Price probed below prior lows, but net taker delta is positive
    if recent_low <= min_prior_low and net_last_delta > 0:
        return "bullish_absorption"

    # Bearish Absorption: Price probed above prior highs, but net taker delta is negative
    if recent_high >= max_prior_high and net_last_delta < 0:
        return "bearish_absorption"

    return None


def cvd_alignment(candles: list[OHLCVCandle], direction: str, window: int = 15) -> tuple[bool, float, str]:
    """
    Check if aggressive taker volume confirms the trade direction.

    Returns:
      (aligns: bool, net_delta: float, reason: str)
    """
    delta = cvd_delta_window(candles, window)
    if delta == 0:
        return True, 0.0, "CVD neutral / no delta data"

    if direction == "long":
        if delta > 0:
            return True, delta, f"CVD confirms buyers (+{delta:,.1f})"
        else:
            return False, delta, f"CVD divergence: net sellers ({delta:,.1f}) while aiming long"
    else:
        if delta < 0:
            return True, delta, f"CVD confirms sellers ({delta:,.1f})"
        else:
            return False, delta, f"CVD divergence: net buyers (+{delta:,.1f}) while aiming short"


def compute_cvd_trend(candles: list[OHLCVCandle]) -> str:
    """
    Summarize current orderflow into a concise trend label:
    - "bullish_absorption"
    - "bearish_absorption"
    - "bullish_delta"
    - "bearish_delta"
    - "neutral"
    """
    absorption = detect_absorption(candles)
    if absorption:
        return absorption
    delta = cvd_delta_window(candles, 15)
    if delta > 0:
        return "bullish_delta"
    elif delta < 0:
        return "bearish_delta"
    return "neutral"
