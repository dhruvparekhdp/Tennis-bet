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
    VOLUME      whether the money agrees with the price
    STRUCTURE   what the swing highs and lows are actually doing
    PATTERN     what the most recent bars did

VOLATILITY is a gate rather than a vote: it can veto, never elect. A quiet
market is not a reason to go long or short — it is a reason not to trade.
"""
from __future__ import annotations

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


def volume_vote(highs, lows, closes, volumes) -> Vote:
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

    # Threshold sits below the 0.5 that VWAP position alone contributes.
    # At `> 0.5` the family abstained in 39 of 40 runs, because money-flow
    # rarely reaches its extremes — the family was present but silent, which
    # is worse than absent since it looked like coverage.
    direction = 1 if score >= 0.5 else (-1 if score <= -0.5 else 0)
    return Vote(Family.VOLUME, direction, min(1.0, abs(score) / 1.5), "; ".join(reasons))


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


def volatility_veto(highs, lows, closes, min_atr_pct: float = 0.00168) -> str | None:
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
    absolute = ind.atr_pct(highs, lows, closes)
    if absolute is not None and absolute < min_atr_pct:
        return (f"ATR is {absolute * 100:.3f}% — below the {min_atr_pct * 100:.3f}% "
                "it costs to open and close")

    pct = ind.volatility_percentile(highs, lows, closes)
    if pct is not None and pct < QUIET_PERCENTILE:
        return f"volatility in the bottom {pct * 100:.0f}% of its own recent range"
    sq = ind.squeeze_on(highs, lows, closes)
    if sq:
        # A squeeze precedes a move but does not say which way. Trading inside
        # one is paying a round trip to find out.
        return "bands are squeezed inside the Keltner channel — a move is coming, direction unknown"
    return None


def evaluate(candles: list[OHLCVCandle], min_atr_pct: float = 0.00168) -> Verdict:
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
    volumes = [c.volume for c in candles]
    atr_value = ind.atr(highs, lows, closes) or 0.0

    votes = [
        trend_vote(highs, lows, closes),
        momentum_vote(highs, lows, closes),
        volume_vote(highs, lows, closes, volumes),
        structure_vote(highs, lows),
        pattern_vote(candles, atr_value),
    ]

    veto = volatility_veto(highs, lows, closes, min_atr_pct)
    if veto:
        return Verdict(None, 0.0, votes=votes, vetoes=[veto])

    long_score = sum(FAMILY_WEIGHT[v.family] * v.weight for v in votes if v.direction > 0)
    short_score = sum(FAMILY_WEIGHT[v.family] * v.weight for v in votes if v.direction < 0)
    if long_score == short_score:
        return Verdict(None, 0.0, votes=votes, vetoes=["families evenly split"])

    direction = "long" if long_score > short_score else "short"
    want = 1 if direction == "long" else -1
    agreeing = sum(1 for v in votes if v.direction == want)
    if agreeing < MIN_AGREEING_FAMILIES:
        return Verdict(None, 0.0, votes=votes,
                       vetoes=[f"only {agreeing} of 5 families agree"])

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
