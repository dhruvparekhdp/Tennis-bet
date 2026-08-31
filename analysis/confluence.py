"""
Confluence scoring: turning many indicators into one honest decision.

The instinct on seeing one analyzer dominate is to add twenty more. That makes
things worse. Twenty analyzers each firing independently produce twenty times
the signals at the same accuracy, and since every trade pays a round trip, more
signals at unchanged accuracy is strictly worse than fewer.

Accuracy comes from AGREEMENT, and only agreement between things that could
have disagreed. RSI, Stochastic and MACD are three views of the same momentum;
counting them as three votes triple-counts one piece of evidence. So indicators
are grouped into families, each family produces at most ONE vote, and
confidence is driven by how many independent families agree.

Six families, each seeing something the others cannot:

    TREND       direction and strength of the prevailing move
    MOMENTUM    speed, and whether it is fading
    VOLATILITY  whether there is enough movement to be worth trading
    VOLUME      whether the money agrees with the price, and whether there is
                enough of it to carry the move
    STRUCTURE   what the swing highs and lows are actually doing
    PATTERN     what the most recent bars did

VOLATILITY is a gate rather than a vote: it can veto, never elect. A quiet
market is not a reason to go long or short — it is a reason not to trade.
Volume works both ways: which side is buying is a vote, but a tape that has
dried up is a gate, for the same reason — thin trading says nothing about
direction and a great deal about whether a target will be reached.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum

from analysis import indicators as ind
from analysis.crypto_state import OHLCVCandle
from analysis.patterns import detect_all


class Family(StrEnum):
    TREND = "trend"
    MOMENTUM = "momentum"
    VOLATILITY = "volatility"
    VOLUME = "volume"
    STRUCTURE = "structure"
    PATTERN = "pattern"


@dataclass(frozen=True)
class Vote:
    family: Family
    direction: int          # +1 long, -1 short, 0 no opinion
    weight: float           # 0..1, how strong this family's view is
    reason: str


@dataclass
class Verdict:
    direction: str | None            # "long" | "short" | None
    confidence: float
    votes: list[Vote] = field(default_factory=list)
    vetoes: list[str] = field(default_factory=list)

    @property
    def agreeing_families(self) -> int:
        if self.direction is None:
            return 0
        want = 1 if self.direction == "long" else -1
        return sum(1 for v in self.votes if v.direction == want)

    @property
    def dissenting_families(self) -> int:
        if self.direction is None:
            return 0
        want = 1 if self.direction == "long" else -1
        return sum(1 for v in self.votes if v.direction == -want)

    def summary(self) -> str:
        parts = [v.reason for v in self.votes if v.direction != 0 and v.reason]
        return " · ".join(parts)


# Families are weighted by how much independent information they carry on a
# short hold. Structure and trend lead because they are the slowest to change
# and hardest to fake; a single candlestick pattern counts for least.
FAMILY_WEIGHT = {
    Family.TREND: 1.0,
    Family.STRUCTURE: 1.0,
    Family.MOMENTUM: 0.8,
    Family.VOLUME: 0.7,
    Family.PATTERN: 0.5,
}

# Below this many agreeing families there is no trade at any score. Two
# families agreeing is a coincidence; three is a reason.
MIN_AGREEING_FAMILIES = 3

# Volatility percentile below which the market is too quiet to bother. This is
# a veto, separate from the absolute cost floor in scalp_levels — that one asks
# "can this move pay a fee", this one asks "is this coin even awake today".
QUIET_PERCENTILE = 0.20


@dataclass(frozen=True)
class ConvictionGate:
    """
    How much agreement a signal must carry before it is worth taking.

    Measured over 240 runs on data with a known direction:

        3 of 5 agree, dissent allowed   79 signals   89.9% correct   (default)
        3 of 5 agree, NO dissent        37 signals  100.0% correct
        4 of 5 agree, dissent allowed    8 signals  100.0% correct
        4 of 5 agree, no dissent         4 signals  100.0% correct

    The useful result is the second row. Forbidding dissent buys the same
    accuracy as demanding a fourth vote while keeping four times as many
    trades — one family actively arguing the other way is stronger evidence
    against a setup than a fourth family merely having no opinion. Absence of
    a vote is not the same as opposition, and the default gate was treating
    them alike.

    Read the 100% figures as "no counter-examples in this sample", not as a
    promise. The data has a genuine direction to find; a real market often
    does not.
    """

    min_agreeing: int = MIN_AGREEING_FAMILIES
    max_dissent: int | None = None
    label: str = "default"

    @classmethod
    def high_conviction(cls) -> ConvictionGate:
        """Few trades, no family arguing against. The row above that pays."""
        return cls(min_agreeing=3, max_dissent=0, label="high_conviction")

    @classmethod
    def strict(cls) -> ConvictionGate:
        """Rarest setups only — four families for, none against."""
        return cls(min_agreeing=4, max_dissent=0, label="strict")


def trend_vote(highs, lows, closes) -> Vote:
    a = ind.adx(highs, lows, closes)
    st = ind.supertrend(highs, lows, closes)
    if a is None or st is None:
        return Vote(Family.TREND, 0, 0.0, "")

    _, st_dir = st
    if not a.is_trending:
        # ADX below 25 means no trend worth following. Saying so is more useful
        # than picking a side from a directional indicator that has no trend to
        # be directional about.
        return Vote(Family.TREND, 0, 0.0, f"no clear trend (ADX {a.adx:.0f})")

    if a.direction != st_dir:
        return Vote(Family.TREND, 0, 0.0, "trend indicators disagree")

    strength = min(1.0, (a.adx - 25.0) / 25.0)
    word = "up" if st_dir > 0 else "down"
    return Vote(Family.TREND, st_dir, strength, f"trending {word} (ADX {a.adx:.0f})")


def momentum_vote(highs, lows, closes) -> Vote:
    """
    One vote from three indicators, because they measure the same thing.

    Divergence is checked at real swing pivots, not by asking whether RSI
    happens to be ticking upward.
    """
    r = ind.rsi(closes)
    m = ind.macd(closes)
    s = ind.stochastic(highs, lows, closes)
    if r is None or m is None or s is None:
        return Vote(Family.MOMENTUM, 0, 0.0, "")

    _, _, hist = m
    k, d = s
    rs = ind.rsi_series(closes)
    offset = len(closes) - len(rs)
    aligned_closes = closes[offset:] if offset > 0 else closes

    bull_div = ind.divergence(aligned_closes, rs, bullish=True)
    bear_div = ind.divergence(aligned_closes, rs, bullish=False)

    score = 0.0
    reasons = []
    if bull_div:
        score += 1.0
        reasons.append("bullish RSI divergence at the lows")
    if bear_div:
        score -= 1.0
        reasons.append("bearish RSI divergence at the highs")
    if r < 30:
        score += 0.5
        reasons.append(f"oversold (RSI {r:.0f})")
    elif r > 70:
        score -= 0.5
        reasons.append(f"overbought (RSI {r:.0f})")
    score += 0.5 if hist > 0 else -0.5
    if k < 20 and k > d:
        score += 0.5
        reasons.append("stochastic turning up from oversold")
    elif k > 80 and k < d:
        score -= 0.5
        reasons.append("stochastic turning down from overbought")

    direction = 1 if score > 0.5 else (-1 if score < -0.5 else 0)
    return Vote(Family.MOMENTUM, direction, min(1.0, abs(score) / 2.0),
                "; ".join(reasons))


# How thin the tape may get before a short hold is not worth attempting.
# Below this the price is drifting on very few trades: the move is easy to
# reverse, the modelled fill is optimistic, and the target is least likely to
# be reached inside the window.
MIN_RELATIVE_VOLUME = 0.60

# Above this the bar carries real participation, and the move behind it is
# more likely to continue than to be one order pushing a thin book.
STRONG_RELATIVE_VOLUME = 1.50


def volume_vote(highs, lows, closes, volumes) -> Vote:
    """
    Whether the money agrees with the price.

    Abstains outright when the feed has no usable per-bar volume. It used to
    vote anyway: on the REST path every bar carried the same rolling 24-hour
    total, so VWAP degenerated into an unweighted average of typical price and
    money flow became a price oscillator wearing a volume label. That produced
    a confident-looking fifth vote out of no volume information at all, and a
    family that always votes is indistinguishable from a family that knows
    something.
    """
    if not ind.has_usable_volume(volumes):
        return Vote(Family.VOLUME, 0, 0.0, "no per-bar volume from this feed")

    m = ind.mfi(highs, lows, closes, volumes)
    vw = ind.vwap(highs, lows, closes, volumes)
    if m is None or vw is None or not closes:
        return Vote(Family.VOLUME, 0, 0.0, "")

    score = 0.0
    reasons = []
    if m < 25:
        score += 1.0
        reasons.append(f"money flow oversold ({m:.0f})")
    elif m > 75:
        score -= 1.0
        reasons.append(f"money flow overbought ({m:.0f})")

    if closes[-1] > vw:
        score += 0.5
        reasons.append("trading above VWAP")
    else:
        score -= 0.5
        reasons.append("trading below VWAP")

    # Which side the volume is actually arriving on. VWAP says where the money
    # traded; this says who is doing the trading now, which is the part that
    # decides whether a move continues.
    vt = ind.volume_trend(closes, volumes)
    if vt is not None and abs(vt) > 0.25:
        score += 0.5 if vt > 0 else -0.5
        reasons.append(f"{'buyers' if vt > 0 else 'sellers'} own "
                       f"{abs(vt) * 100:.0f}% of the volume")

    # Participation does not pick a side, so it scales the family's weight
    # rather than adding to the score. A correct read on a dead tape is still
    # a weak reason to pay a round trip.
    rel = ind.relative_volume(volumes)
    if rel is not None:
        if rel >= STRONG_RELATIVE_VOLUME:
            reasons.append(f"volume {rel:.1f}x its recent average")
        elif rel < MIN_RELATIVE_VOLUME:
            reasons.append(f"thin tape, volume {rel:.1f}x its recent average")

    # Threshold sits below the 0.5 that VWAP position alone contributes.
    # At `> 0.5` the family abstained in 39 of 40 runs, because money-flow
    # rarely reaches its extremes — the family was present but silent, which
    # is worse than absent since it looked like coverage.
    direction = 1 if score >= 0.5 else (-1 if score <= -0.5 else 0)
    weight = min(1.0, abs(score) / 2.0)
    if rel is not None:
        # 0.5x participation halves the family's say; 1.5x restores it in full.
        weight *= max(0.4, min(1.0, rel))
    return Vote(Family.VOLUME, direction, weight, "; ".join(reasons))


def thin_volume_veto(volumes, min_relative: float = MIN_RELATIVE_VOLUME) -> str | None:
    """
    Stand aside when the tape has dried up. Never elects a direction.

    A short-horizon target assumes the market keeps trading at something like
    its recent rate. When it does not, three things go wrong at once: the move
    is unlikely to arrive inside the window, the fill is worse than modelled,
    and the same few orders that drifted the price up can drift it back.

    Silent when the feed carries no usable per-bar volume. A missing
    measurement is not evidence of a thin market, and vetoing on absent data
    would have silenced every REST-fed symbol.
    """
    rel = ind.relative_volume(volumes)
    if rel is None:
        return None
    if rel < min_relative:
        return (f"volume is {rel:.2f}x its recent average — too few trades to "
                f"carry a move inside the window")
    return None


def structure_vote(highs, lows) -> Vote:
    s = ind.trend_structure(highs, lows)
    if s == 0:
        return Vote(Family.STRUCTURE, 0, 0.0, "")
    word = "higher highs and higher lows" if s > 0 else "lower highs and lower lows"
    return Vote(Family.STRUCTURE, s, 0.8, word)


def pattern_vote(candles: list[OHLCVCandle], atr_value: float) -> Vote:
    found = [p for p in detect_all(candles, atr_value) if p.direction != 0]
    if not found:
        return Vote(Family.PATTERN, 0, 0.0, "")
    score = sum(p.direction * p.strength for p in found)
    if abs(score) < 0.3:
        return Vote(Family.PATTERN, 0, 0.0, "patterns conflict")
    best = max(found, key=lambda p: p.strength)
    return Vote(Family.PATTERN, 1 if score > 0 else -1,
                min(1.0, abs(score)), best.description)


def volatility_veto(highs, lows, closes, min_atr_pct: float = 0.00168,
                    horizon_minutes: float = 30.0) -> str | None:
    """
    Returns a reason to stand aside, or None. Never elects a direction.

    Two separate questions, and both must pass:

      Absolute — can a move of this size pay for a round trip at all? The
      default is the crypto cost floor. Without this check a market whose ATR
      is 0.037% still scores well, because it is volatile *for itself*.

      Relative — is this coin awake compared with its own recent behaviour? A
      coin that always moves 3% is not unusually volatile today because it
      moved 3%.
    """
    # Over the holding window, not per bar. Holding a one-minute ATR against a
    # per-trade cost is a unit mismatch, and it is what reduced the watchlist
    # to whichever single coin happened to be most volatile that hour.
    absolute = ind.atr_pct(highs, lows, closes)
    if absolute is not None:
        reachable = absolute * math.sqrt(max(1.0, horizon_minutes))
        if reachable < min_atr_pct:
            return (f"could move {reachable * 100:.3f}% in {horizon_minutes:.0f} min — "
                    f"under the {min_atr_pct * 100:.3f}% a round trip costs")

    pct = ind.volatility_percentile(highs, lows, closes)
    if pct is not None and pct < QUIET_PERCENTILE:
        return f"volatility in the bottom {pct * 100:.0f}% of its own recent range"
    sq = ind.squeeze_on(highs, lows, closes)
    if sq:
        # A squeeze precedes a move but does not say which way. Trading inside
        # one is paying a round trip to find out.
        return "bands are squeezed inside the Keltner channel — a move is coming, direction unknown"
    return None


def evaluate(candles: list[OHLCVCandle], min_atr_pct: float = 0.00168,
             horizon_minutes: float = 30.0,
             min_agreeing: int = MIN_AGREEING_FAMILIES,
             max_dissent: int | None = None) -> Verdict:
    """
    Weigh every family and return one decision.

    Confidence is built from the share of available weight that agrees, then
    penalised for dissent — so eight indicators agreeing with two against is
    worth materially less than eight agreeing with none against.
    """
    if len(candles) < 60:
        return Verdict(None, 0.0, vetoes=["not enough history"])

    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    atr_value = ind.atr(highs, lows, closes) or 0.0

    # Volume gets CLOSED bars only, and this is not a detail.
    #
    # The last kline is the minute currently in progress. Its high, low and
    # close are real — they are what has happened so far — so the price
    # families keep it. Its volume is not comparable to anything: a bar three
    # seconds old has traded almost nothing, so relative volume comes out at
    # 0.00x and the thin-tape veto fires on every symbol at once. That is
    # exactly what the live board showed — five coins refused for "0.00x its
    # recent average" while the venue was trading normally.
    closed = [c for c in candles if c.is_closed]
    v_highs = [c.high for c in closed]
    v_lows = [c.low for c in closed]
    v_closes = [c.close for c in closed]
    volumes = [c.volume for c in closed]

    votes = [
        trend_vote(highs, lows, closes),
        momentum_vote(highs, lows, closes),
        volume_vote(v_highs, v_lows, v_closes, volumes),
        structure_vote(highs, lows),
        pattern_vote(candles, atr_value),
    ]

    vetoes = [v for v in (volatility_veto(highs, lows, closes, min_atr_pct,
                                          horizon_minutes),
                          thin_volume_veto(volumes)) if v]
    if vetoes:
        return Verdict(None, 0.0, votes=votes, vetoes=vetoes)

    long_score = sum(FAMILY_WEIGHT[v.family] * v.weight for v in votes if v.direction > 0)
    short_score = sum(FAMILY_WEIGHT[v.family] * v.weight for v in votes if v.direction < 0)
    if long_score == short_score:
        return Verdict(None, 0.0, votes=votes, vetoes=["families evenly split"])

    direction = "long" if long_score > short_score else "short"
    want = 1 if direction == "long" else -1
    agreeing = sum(1 for v in votes if v.direction == want)
    against = sum(1 for v in votes if v.direction == -want)
    if agreeing < min_agreeing:
        return Verdict(None, 0.0, votes=votes,
                       vetoes=[f"only {agreeing} of 5 families agree"])
    if max_dissent is not None and against > max_dissent:
        return Verdict(None, 0.0, votes=votes,
                       vetoes=[f"{against} families disagree"])

    winner, loser = max(long_score, short_score), min(long_score, short_score)
    total_available = sum(FAMILY_WEIGHT.values())
    share = winner / total_available
    dissent_penalty = loser / total_available

    # 0.55 floor because a signal that survived a three-family agreement test is
    # already better than a coin flip; the ceiling is 0.92 because nothing here
    # justifies presenting a near-certainty.
    confidence = 0.55 + share * 0.45 - dissent_penalty * 0.30
    confidence = max(0.0, min(0.92, confidence))

    return Verdict(direction, round(confidence, 4), votes=votes)
