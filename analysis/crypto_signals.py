from __future__ import annotations

from datetime import UTC, datetime

import structlog

from analysis import indicators as ind
from analysis.confluence import MIN_AGREEING_FAMILIES as MIN_AGREEING
from analysis.confluence import ConvictionGate, evaluate
from analysis.crypto_signal import CryptoSignal, compute_crypto_stake
from analysis.crypto_state import CryptoState
from analysis.patterns import range_breakout
from analysis.scalp_levels import (
    HORIZONS_MINUTES,
    NoTrade,
    ScalpConfig,
    scalp_levels,
)
from config.settings import settings

log = structlog.get_logger()

# One cost frame for every analyzer. Levels used to be set as `price +/- atr*k`
# with no reference to what a round trip costs, which is how the dashboard came
# to show 0.05% targets against a 0.118% fee. Cost is now the floor and
# volatility only decides whether a trade exists at all.
SCALP = (ScalpConfig.high_conviction() if settings.high_conviction_only
         else ScalpConfig())

# How much agreement a signal needs. High conviction forbids any family from
# arguing the other way, which measured as accurate as demanding a fourth vote
# while keeping four times the trades.
GATE = (ConvictionGate.high_conviction() if settings.high_conviction_only
        else ConvictionGate())


def _format_timeframe(horizon_minutes: float, fallback: str = "30m") -> str:
    h = int(round(horizon_minutes))
    if h == 60:
        return "1h"
    elif h == 240:
        return "4h"
    elif h > 0:
        return f"{h}m"
    return fallback


def _emit(
    state: CryptoState,
    *,
    direction: str,
    signal_type: str,
    trigger_desc: str,
    confidence: float,
    timeframe: str,
    atr_target_multiple: float = 1.0,
    reward_risk: float = 1.0,
    extra_indicators: str = "",
) -> CryptoSignal | None:
    """
    Apply the shared level policy and build the signal, or refuse it.

    Every analyzer routes through here, so there is exactly one definition of
    what a tradeable setup looks like. An analyzer's job is to say *whether*
    and *which way* — never how far, which is a question about cost and
    volatility rather than about the pattern that fired.
    """
    price = state.current_price
    if price <= 0:
        return None

    # No synthetic fallback. A zero ATR means the candle feed is flat, and
    # inventing a volatility number there manufactures trades out of nothing —
    # which is exactly what the old `atr or price * 0.015` fallback did.
    if state.atr_14 <= 0:
        log.debug("scalp.no_atr", symbol=state.symbol, signal_type=signal_type)
        return None

    # Cost is per-market: gold is five times cheaper to trade than ether, so
    # holding both to the same floor would refuse profitable gold scalps.
    cfg = SCALP.for_symbol(state.symbol)

    # Offer the setup the shortest horizon first. A move reachable in ten
    # minutes is a better setup than one needing thirty, and recording which
    # window it qualified under makes the two directly comparable in the feed
    # rather than being a silent internal assumption.
    # How long a bar actually is, measured rather than assumed. The ATR is
    # per BAR, and the projection scales it by sqrt(horizon / bar) — so a feed
    # returning five-minute candles, or a history with gaps from an instance
    # that slept, makes "within 15m" mean something else entirely.
    bar_minutes = ind.median_bar_minutes([c.timestamp for c in state.candles_1m])

    levels: object = NoTrade.TOO_QUIET
    for horizon in HORIZONS_MINUTES:
        levels = scalp_levels(
            entry=price,
            is_long=direction == "long",
            atr_pct=state.atr_14 / price,
            cfg=cfg,
            symbol=state.symbol,
            reward_risk=reward_risk,
            atr_target_multiple=atr_target_multiple,
            horizon_minutes=horizon,
            bar_minutes=bar_minutes,
        )
        if not isinstance(levels, NoTrade):
            break

    if isinstance(levels, NoTrade):
        log.debug("scalp.refused", symbol=state.symbol,
                  signal_type=signal_type, reason=levels.value)
        return None

    # Edge is what survives the round trip, not the raw move. Sizing off the
    # gross move is how a losing trade looks attractive.
    edge_pct = round(levels.edge_after_costs_pct * 100.0, 3)
    # Participation is recorded on every signal, not just the volume ones, so
    # the audit page can ask whether the wrong calls share a thin tape.
    rel = ind.relative_volume([c.volume for c in state.candles_1m])
    vol_part = f"Vol {rel:.1f}x" if rel is not None else "Vol n/a"
    summary = (f"RSI {state.rsi_14:.1f} | ATR {state.atr_14 / price * 100:.2f}% | "
               f"{vol_part} | 24h {state.price_change_24h_pct:+.1f}%")
    if extra_indicators:
        summary = f"{extra_indicators} | {summary}"

    tf_label = _format_timeframe(levels.horizon_minutes, timeframe)

    if price <= 0.001 or levels.target <= 0 or levels.stop <= 0:
        return None
    if abs(levels.target - price) / price > 0.50:
        return None

    return CryptoSignal(
        symbol=state.symbol,
        signal_type=signal_type,
        direction=direction,
        trigger_description=trigger_desc,
        confidence=round(confidence, 2),
        current_price=price,
        target_price=levels.target,
        stop_loss=levels.stop,
        edge_pct=edge_pct,
        stake_pct=compute_crypto_stake(edge_pct, confidence),
        timeframe=tf_label,
        sentiment_score=state.sentiment_score,
        indicators_summary=summary,
        timestamp=datetime.now(UTC),
    )


class RSIDivergenceAnalyzer:
    """Detects RSI divergence (momentum vs price discrepancies) indicating upcoming reversals."""

    def analyze(self, state: CryptoState) -> CryptoSignal | None:
        if len(state.candles_1m) < 20 or state.current_price <= 0:
            return None

        div = state.rsi_divergence(lookback=20)
        if not div:
            return None

        if div == "bullish":
            direction = "long"
            confidence = min(0.85, 0.65 + (35.0 - min(state.rsi_14, 35.0)) * 0.01)
            trigger_desc = ("Price dipped to a new low, but selling pressure is fading "
                            "— often an early reversal signal.")
        else:
            direction = "short"
            confidence = min(0.85, 0.65 + (max(state.rsi_14, 65.0) - 65.0) * 0.01)
            trigger_desc = ("Price hit a new high, but buying pressure is fading "
                            "— often an early reversal signal.")

        return _emit(
            state, direction=direction, signal_type="rsi_divergence",
            trigger_desc=trigger_desc, confidence=confidence, timeframe="1h",
            atr_target_multiple=1.2, reward_risk=1.0,
        )


class VolumeSpikeAnalyzer:
    """Flags volume surges with consolidating or coiled price action (accumulation/distribution)."""

    MIN_VOLUME_RATIO = 2.8

    def analyze(self, state: CryptoState) -> CryptoSignal | None:
        if len(state.candles_1m) < 15 or state.current_price <= 0:
            return None

        # Prefer the per-bar measure and say which one fired. The old version
        # tested a per-bar spike and then printed `state.volume_ratio`, a
        # 24-hour figure, in the alert — two different numbers presented as
        # one, so a message could claim 3.4x while the thing that triggered
        # was something else entirely.
        volumes = [c.volume for c in state.candles_1m]
        rel = ind.relative_volume(volumes)
        if rel is not None:
            ratio, basis = rel, "its recent average"
        elif state.volume_ratio > 0:
            ratio, basis = state.volume_ratio, "its 24h average"
        else:
            return None
        if ratio < self.MIN_VOLUME_RATIO:
            return None

        # Direction from the candle body: a volume surge confirms whichever way
        # the bar closed, it does not pick the direction itself.
        last_candle = state.candles_1m[-1]
        is_bullish = last_candle.close >= last_candle.open

        return _emit(
            state,
            direction="long" if is_bullish else "short",
            signal_type="volume_spike",
            trigger_desc=(f"Trading volume just spiked to {ratio:.1f}x {basis} "
                          "— a surge like this often kicks off a bigger move."),
            confidence=0.68,
            timeframe="30m",
            atr_target_multiple=1.5,
            reward_risk=1.0,
            extra_indicators=f"Vol {ratio:.1f}x",
        )


class BollingerSqueezeAnalyzer:
    """Volatility compression followed by a genuine break out of it."""

    def analyze(self, state: CryptoState) -> CryptoSignal | None:
        candles = state.candles_1m
        if len(candles) < 40 or state.current_price <= 0:
            return None

        highs = [c.high for c in candles]
        lows = [c.low for c in candles]
        closes = [c.close for c in candles]

        # The squeeze is Bollinger inside Keltner, not band width below a
        # constant. The old `bandwidth > 0.025` test was scale-dependent: 2.5%
        # is a dead market for a small coin and a violent one for BTC, so the
        # same number meant two different things depending on the symbol.
        # It must have been squeezed RECENTLY and be breaking out NOW — a
        # squeeze that is still on says a move is coming but not which way.
        was_squeezed = ind.squeeze_on(highs[:-1], lows[:-1], closes[:-1])
        still_squeezed = ind.squeeze_on(highs, lows, closes)
        if not was_squeezed or still_squeezed:
            return None

        atr_value = ind.atr(highs, lows, closes) or 0.0
        breakout = range_breakout(candles, lookback=20, buffer_atr=atr_value * 0.25)
        if breakout is None or breakout.direction == 0:
            return None

        return _emit(
            state,
            direction="long" if breakout.direction > 0 else "short",
            signal_type="bollinger_squeeze",
            trigger_desc=("Price coiled into a tight squeeze and has just broken out "
                          "of its recent range — compression like this often releases "
                          "into a real move."),
            confidence=0.70,
            timeframe="4h",
            atr_target_multiple=1.4,
            reward_risk=1.0,
            extra_indicators="squeeze released",
        )


class SentimentShiftAnalyzer:
    """Fires when high-conviction FinBERT/CryptoPanic news sentiment diverges from price action."""

    def analyze(self, state: CryptoState) -> CryptoSignal | None:
        if abs(state.sentiment_score) < 0.35 or state.current_price <= 0:
            return None

        if state.sentiment_score >= 0.35 and state.rsi_14 < 60:
            direction = "long"
            confidence = min(0.82, 0.62 + state.sentiment_score * 0.20)
            trigger_desc = (f"News coverage right now is strongly positive "
                            f"({state.sentiment_news_count} recent articles).")
        elif state.sentiment_score <= -0.35 and state.rsi_14 > 40:
            direction = "short"
            confidence = min(0.82, 0.62 + abs(state.sentiment_score) * 0.20)
            trigger_desc = (f"News coverage right now is strongly negative "
                            f"({state.sentiment_news_count} recent articles).")
        else:
            return None

        return _emit(
            state, direction=direction, signal_type="sentiment_shift",
            trigger_desc=trigger_desc, confidence=confidence, timeframe="1d",
            atr_target_multiple=2.0, reward_risk=1.0,
            extra_indicators=(f"Sentiment {state.sentiment_score:+.2f} | "
                              f"News {state.sentiment_news_count}"),
        )


class ConfluenceAnalyzer:
    """
    The primary analyzer: trades only where independent evidence agrees.

    The other four analyzers each look at one thing and fire on it. This one
    asks five families — trend, momentum, volume, structure and pattern — and
    requires at least three to agree before it will say anything. Volatility is
    a veto rather than a vote, because a quiet market is not a reason to go
    either way; it is a reason to stand aside.

    Confidence here is earned rather than assigned: it comes from the share of
    available evidence that agrees, penalised by whatever disagrees. The other
    analyzers hand out fixed numbers like 0.68 and 0.70, which look like
    measurements but are not.
    """

    def analyze(self, state: CryptoState) -> CryptoSignal | None:
        if len(state.candles_1m) < 60 or state.current_price <= 0:
            return None

        cfg = SCALP.for_symbol(state.symbol)
        # The longest horizon on offer, spelled out rather than left to a
        # default. This veto is a coarse "is a trade possible at all" filter;
        # the per-horizon test that actually decides the levels happens in
        # _emit, which tries the short window first. Vetoing at the short
        # window here would refuse setups that are perfectly good at the long
        # one before they were ever offered it.
        verdict = evaluate(state.candles_1m, min_atr_pct=cfg.cost_floor_pct,
                           horizon_minutes=HORIZONS_MINUTES[-1],
                           min_agreeing=GATE.min_agreeing,
                           max_dissent=GATE.max_dissent)
        if verdict.direction is None:
            if verdict.vetoes:
                log.debug("confluence.no_trade", symbol=state.symbol,
                          reason=verdict.vetoes[0])
            return None

        agree = verdict.agreeing_families
        against = verdict.dissenting_families
        detail = verdict.summary()
        trigger = (f"{agree} of 5 independent checks agree on a "
                   f"{verdict.direction}"
                   + (f", {against} against" if against else "")
                   + (f" — {detail}" if detail else "."))

        # Target multiple scales with agreement: more independent confirmation
        # is a reason to expect a larger move, not merely a bigger position.
        multiple = 1.0 + 0.25 * max(0, agree - MIN_AGREEING)
        return _emit(
            state,
            direction=verdict.direction,
            signal_type="confluence",
            trigger_desc=trigger,
            confidence=verdict.confidence,
            timeframe="1h",
            atr_target_multiple=multiple,
            reward_risk=1.0,
            extra_indicators=f"{agree}/5 agree",
        )
