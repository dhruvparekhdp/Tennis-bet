"""
Candlestick and chart-pattern detection.

Patterns are weak evidence on their own — an engulfing bar in the middle of a
range means nothing. They earn their weight only in context, which is why every
detector here returns a plain direction and lets the confluence layer decide
whether it matters. None of them are allowed to fire a trade by themselves.

All thresholds are expressed relative to the bar's own range or to ATR, never
in absolute price, so the same code works on a $1 coin and on BTC.
"""
from __future__ import annotations

from dataclasses import dataclass

from analysis.crypto_state import OHLCVCandle
from analysis.indicators import swing_pivots


@dataclass(frozen=True)
class Pattern:
    name: str
    direction: int          # +1 bullish, -1 bearish
    strength: float         # 0..1, how clean the example is
    description: str


def _body(c: OHLCVCandle) -> float:
    return abs(c.close - c.open)


def _range(c: OHLCVCandle) -> float:
    return c.high - c.low


def _upper_wick(c: OHLCVCandle) -> float:
    return c.high - max(c.open, c.close)


def _lower_wick(c: OHLCVCandle) -> float:
    return min(c.open, c.close) - c.low


# ── single and two-bar candlestick patterns ───────────────────────────────

def engulfing(candles: list[OHLCVCandle]) -> Pattern | None:
    """
    The current body fully covers the previous body, in the opposite direction.

    Requires the previous bar to have a real body: a body that engulfs a doji
    is not an engulfing pattern, it is one bar next to nothing.
    """
    if len(candles) < 2:
        return None
    prev, cur = candles[-2], candles[-1]
    if _range(prev) <= 0 or _body(prev) < _range(prev) * 0.1:
        return None

    bull = (cur.close > cur.open and prev.close < prev.open
            and cur.close >= prev.open and cur.open <= prev.close)
    bear = (cur.close < cur.open and prev.close > prev.open
            and cur.close <= prev.open and cur.open >= prev.close)
    if not (bull or bear):
        return None

    strength = min(1.0, _body(cur) / _body(prev) / 2.0) if _body(prev) else 0.5
    return Pattern("engulfing", 1 if bull else -1, strength,
                   "the last bar completely reversed the one before it")


def pin_bar(candles: list[OHLCVCandle], wick_ratio: float = 2.0) -> Pattern | None:
    """
    A long wick rejecting one side — price went there and was pushed back.

    The wick must be at least `wick_ratio` times the body and the opposite wick
    must be small, otherwise a plain wide bar qualifies.
    """
    if not candles:
        return None
    c = candles[-1]
    rng, body = _range(c), _body(c)
    if rng <= 0 or body <= 0:
        return None
    up, low = _upper_wick(c), _lower_wick(c)

    if low >= body * wick_ratio and low > up * 2:
        return Pattern("pin_bar", 1, min(1.0, low / rng),
                       "price was pushed down and rejected — buyers defended it")
    if up >= body * wick_ratio and up > low * 2:
        return Pattern("pin_bar", -1, min(1.0, up / rng),
                       "price was pushed up and rejected — sellers defended it")
    return None


def inside_bar(candles: list[OHLCVCandle]) -> Pattern | None:
    """
    Full range contained by the previous bar: compression, direction unknown.

    Deliberately returns direction 0 — it is a volatility statement, not a
    directional one, and treating it as directional is a common way to be
    confidently wrong.
    """
    if len(candles) < 2:
        return None
    prev, cur = candles[-2], candles[-1]
    if cur.high <= prev.high and cur.low >= prev.low and _range(prev) > 0:
        return Pattern("inside_bar", 0, 1.0 - _range(cur) / _range(prev),
                       "the market coiled inside the previous bar's range")
    return None


def three_bar_reversal(candles: list[OHLCVCandle]) -> Pattern | None:
    """Two bars one way, then a bar closing past both — a short, sharp turn."""
    if len(candles) < 3:
        return None
    a, b, c = candles[-3], candles[-2], candles[-1]
    down_then_up = (a.close < a.open and b.close < b.open
                    and c.close > c.open and c.close > max(a.open, b.open))
    up_then_down = (a.close > a.open and b.close > b.open
                    and c.close < c.open and c.close < min(a.open, b.open))
    if down_then_up:
        return Pattern("three_bar_reversal", 1, 0.7, "two down bars fully reversed")
    if up_then_down:
        return Pattern("three_bar_reversal", -1, 0.7, "two up bars fully reversed")
    return None


# ── chart structure ───────────────────────────────────────────────────────

def range_breakout(candles: list[OHLCVCandle], lookback: int = 20,
                   buffer_atr: float = 0.0) -> Pattern | None:
    """
    Close beyond the recent range, with an optional ATR buffer.

    The buffer exists because an unbuffered breakout test fires on every bar
    that ticks one cent past the range, which on a noisy feed is most of them.
    """
    if len(candles) < lookback + 1:
        return None
    window = candles[-lookback - 1:-1]
    hi = max(c.high for c in window)
    lo = min(c.low for c in window)
    cur = candles[-1]
    if hi <= lo:
        return None

    if cur.close > hi + buffer_atr:
        return Pattern("range_breakout", 1, min(1.0, (cur.close - hi) / (hi - lo)),
                       f"broke above a {lookback}-bar range")
    if cur.close < lo - buffer_atr:
        return Pattern("range_breakout", -1, min(1.0, (lo - cur.close) / (hi - lo)),
                       f"broke below a {lookback}-bar range")
    return None


def double_top_bottom(candles: list[OHLCVCandle], tolerance: float = 0.003,
                      left: int = 2, right: int = 2) -> Pattern | None:
    """
    Two swing points at nearly the same level — a level that has held twice.

    Tolerance is fractional, so "nearly the same" scales with price instead of
    meaning something different on every instrument.
    """
    if len(candles) < 12:
        return None
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]

    hp = swing_pivots(highs, left, right, find_highs=True)
    if len(hp) >= 2:
        a, b = highs[hp[-2]], highs[hp[-1]]
        if a > 0 and abs(b - a) / a <= tolerance:
            return Pattern("double_top", -1, 1.0 - abs(b - a) / a / tolerance,
                           "price failed twice at the same level")

    lp = swing_pivots(lows, left, right, find_highs=False)
    if len(lp) >= 2:
        a, b = lows[lp[-2]], lows[lp[-1]]
        if a > 0 and abs(b - a) / a <= tolerance:
            return Pattern("double_bottom", 1, 1.0 - abs(b - a) / a / tolerance,
                           "price held twice at the same level")
    return None


def detect_all(candles: list[OHLCVCandle], atr_value: float = 0.0) -> list[Pattern]:
    """Every pattern present on the latest bar. Direction 0 entries are kept —
    a compression reading is information even though it picks no side."""
    found = []
    for fn in (engulfing, pin_bar, inside_bar, three_bar_reversal, double_top_bottom):
        p = fn(candles)
        if p is not None:
            found.append(p)
    b = range_breakout(candles, buffer_atr=atr_value * 0.25)
    if b is not None:
        found.append(b)
    return found
