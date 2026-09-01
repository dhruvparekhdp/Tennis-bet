"""
Where a market is likely to be, and how sure that is.

What this answers
-----------------
"ETH is 2453 — will it be 2475 or 2425, and roughly when?" The signal engine
answers a narrower question: is there a trade here right now worth its costs.
Most of the time the honest answer to that is no, which leaves the board empty
and says nothing about the market. This says something about every market, all
the time, and is explicit about how much of it is knowable.

The shape of an honest forecast
-------------------------------
A single predicted price is a lie of precision. Nobody can tell you ETH will
be 2474.86 in four hours. What can be said, and defended:

  * how far this market typically travels in that time — its own recent range,
    projected by the square-root-of-time rule
  * which way the weight of evidence leans, from the same five independent
    families the signal engine votes with
  * therefore a CENTRE that is shifted from spot by the lean, and a BAND that
    is the typical travel either side of it

So the answer is "2453, likely between 2426 and 2480 over four hours, leaning
up" — a range with a direction, which is the most any of this supports. The
band is roughly one standard deviation: the price should land inside it about
two times in three, and outside it the other time.

Deliberately separate from the signal path. A forecast is not a trade
recommendation — it does not know what a round trip costs and it never asks
whether the move clears one. Signals stay strict; this stays informative.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from analysis import indicators as ind
from analysis.confluence import FAMILY_WEIGHT, evaluate

# What the page shows. Short enough to act on, long enough to matter.
DEFAULT_HORIZONS: tuple[int, ...] = (60, 240, 1440)

HORIZON_LABEL = {60: "1h", 240: "4h", 1440: "24h"}

# A lean this weak is noise dressed as an opinion.
MIN_LEAN = 0.10

# How much of the typical travel the lean is allowed to claim. At 1.0 a
# unanimous read would predict a full standard deviation of drift, which no
# indicator set on earth earns. A third is already generous.
MAX_DRIFT_SHARE = 0.35


@dataclass(frozen=True)
class Forecast:
    """One market, one horizon."""

    symbol: str
    price: float
    horizon_minutes: int
    label: str
    typical_move_pct: float     # one standard deviation of travel, in percent
    lean: float                 # -1..+1, weight of evidence
    centre: float               # price shifted by the lean
    low: float
    high: float
    confidence: float           # 0..1, how much agreement is behind the lean

    @property
    def direction(self) -> str:
        if self.lean > MIN_LEAN:
            return "up"
        return "down" if self.lean < -MIN_LEAN else "flat"

    @property
    def change_pct(self) -> float:
        return (self.centre - self.price) / self.price * 100 if self.price else 0.0

    def as_dict(self) -> dict:
        return {
            "horizon_minutes": self.horizon_minutes,
            "label": self.label,
            "centre": round(self.centre, 8),
            "low": round(self.low, 8),
            "high": round(self.high, 8),
            "change_pct": round(self.change_pct, 3),
            "band_pct": round(self.typical_move_pct, 3),
            "direction": self.direction,
            "lean": round(self.lean, 3),
            "confidence": round(self.confidence, 3),
        }


def lean_from_votes(votes) -> tuple[float, float]:
    """
    Weight of evidence as -1..+1, and how much of the panel spoke.

    The same family weights the signal gate uses, so the forecast and the
    signals cannot disagree about what the indicators said — only about
    whether it is worth paying to act on it.
    """
    if not votes:
        return 0.0, 0.0
    total = sum(FAMILY_WEIGHT.values())
    if total <= 0:
        return 0.0, 0.0
    signed = sum(FAMILY_WEIGHT.get(v.family, 0.0) * v.weight * v.direction
                 for v in votes)
    spoke = sum(FAMILY_WEIGHT.get(v.family, 0.0) * v.weight
                for v in votes if v.direction != 0)
    return max(-1.0, min(1.0, signed / total)), min(1.0, spoke / total)


def forecast_symbol(state, horizons: tuple[int, ...] = DEFAULT_HORIZONS,
                    bar_minutes: float | None = None) -> list[Forecast]:
    """
    A band and a lean for each horizon, or an empty list if we cannot say.

    Empty is a real answer and is returned rather than a zero: with no candles
    or no measurable range there is nothing to project, and a forecast of "no
    change" would be a claim we have not earned.
    """
    price = getattr(state, "current_price", 0.0) or 0.0
    candles = getattr(state, "candles_1m", []) or []
    if price <= 0 or len(candles) < 30:
        return []

    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    atr_pct = ind.atr_pct(highs, lows, closes)
    if not atr_pct or atr_pct <= 0:
        return []

    if bar_minutes is None:
        bar_minutes = ind.median_bar_minutes([c.timestamp for c in candles])
    bar_minutes = max(bar_minutes, 1e-6)

    verdict = evaluate(candles, min_atr_pct=0.0)
    lean, spoke = lean_from_votes(verdict.votes)
    # A veto is not a direction, but it IS a reason to trust the lean less.
    if verdict.vetoes:
        lean *= 0.5
        spoke *= 0.5

    out = []
    for minutes in horizons:
        bars = max(1.0, minutes / bar_minutes)
        typical = atr_pct * math.sqrt(bars)
        drift = typical * lean * MAX_DRIFT_SHARE
        centre = price * (1 + drift)
        band = price * typical
        out.append(Forecast(
            symbol=getattr(state, "symbol", ""),
            price=price,
            horizon_minutes=minutes,
            label=HORIZON_LABEL.get(minutes, f"{minutes}m"),
            typical_move_pct=typical * 100,
            lean=lean,
            centre=centre,
            low=centre - band,
            high=centre + band,
            confidence=round(0.5 + spoke * 0.4, 3),
        ))
    return out


# ── surges ────────────────────────────────────────────────────────────────
#
# The forecast describes an ordinary session. A surge is the session stopping
# being ordinary, and it is worth saying out loud the moment it happens rather
# than waiting for a setup to clear a cost floor.

SURGE_VOLUME_MULTIPLE = 3.0
SURGE_MOVE_SIGMAS = 2.5


@dataclass(frozen=True)
class Surge:
    symbol: str
    kind: str            # "volume" | "price" | "both"
    detail: str
    magnitude: float     # multiples of normal, for ranking
    direction: int       # +1 / -1 / 0


def detect_surge(state, volume_multiple: float = SURGE_VOLUME_MULTIPLE,
                 move_sigmas: float = SURGE_MOVE_SIGMAS) -> Surge | None:
    """
    Is this market doing something unusual right now, by its own standards?

    Both tests are relative to the instrument. An absolute threshold would
    fire constantly on the lively coins and never on the quiet ones, which is
    the same mistake as holding a one-minute range against a fixed cost.

    Closed bars only for volume: the minute in progress has traded almost
    nothing, so including it reports every market as dead.
    """
    candles = getattr(state, "candles_1m", []) or []
    if len(candles) < 30:
        return None
    closed = [c for c in candles if c.is_closed]
    if len(closed) < 25:
        return None

    reasons: list[str] = []
    kinds: list[str] = []
    magnitude = 0.0
    direction = 0

    rel = ind.relative_volume([c.volume for c in closed])
    if rel is not None and rel >= volume_multiple:
        kinds.append("volume")
        reasons.append(f"volume {rel:.1f}x its recent average")
        magnitude = max(magnitude, rel / volume_multiple)

    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    atr_pct = ind.atr_pct(highs, lows, closes)
    last = closed[-1]
    if atr_pct and atr_pct > 0 and last.open > 0:
        move = (last.close - last.open) / last.open
        sigmas = abs(move) / atr_pct
        if sigmas >= move_sigmas:
            kinds.append("price")
            direction = 1 if move > 0 else -1
            reasons.append(f"a {abs(move) * 100:.2f}% bar, {sigmas:.1f}x its normal range")
            magnitude = max(magnitude, sigmas / move_sigmas)

    if not kinds:
        return None
    kind = "both" if len(kinds) > 1 else kinds[0]
    return Surge(symbol=getattr(state, "symbol", ""), kind=kind,
                 detail=" and ".join(reasons), magnitude=round(magnitude, 2),
                 direction=direction)
