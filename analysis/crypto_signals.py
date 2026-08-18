from __future__ import annotations

from datetime import UTC, datetime

import structlog

from analysis.crypto_signal import CryptoSignal, compute_crypto_stake
from analysis.crypto_state import CryptoState
from analysis.scalp_levels import NoTrade, ScalpConfig, scalp_levels

log = structlog.get_logger()

# One cost frame for every analyzer. Levels used to be set as `price +/- atr*k`
# with no reference to what a round trip costs, which is how the dashboard came
# to show 0.05% targets against a 0.118% fee. Cost is now the floor and
# volatility only decides whether a trade exists at all.
SCALP = ScalpConfig()


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
    levels = scalp_levels(
        entry=price,
        is_long=direction == "long",
        atr_pct=state.atr_14 / price,
        cfg=cfg,
        symbol=state.symbol,
        reward_risk=reward_risk,
        atr_target_multiple=atr_target_multiple,
    )
    if isinstance(levels, NoTrade):
        log.debug("scalp.refused", symbol=state.symbol,
                  signal_type=signal_type, reason=levels.value)
        return None

    # Edge is what survives the round trip, not the raw move. Sizing off the
    # gross move is how a losing trade looks attractive.
    edge_pct = round(levels.edge_after_costs_pct * 100.0, 3)
    summary = (f"RSI {state.rsi_14:.1f} | ATR {state.atr_14 / price * 100:.2f}% | "
               f"24h {state.price_change_24h_pct:+.1f}%")
    if extra_indicators:
        summary = f"{extra_indicators} | {summary}"

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
        timeframe=timeframe,
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

        # Check recent 5-candle volume vs baseline
        recent_vols = [c.volume for c in state.candles_1m[-5:]]
        avg_recent_vol = sum(recent_vols) / len(recent_vols) if recent_vols else 0.0

        per_bar_baseline = state.volume_24h_avg / 288.0 if state.volume_24h_avg > 0 else 0.0
        spiking = (per_bar_baseline > 0
                   and avg_recent_vol / per_bar_baseline >= self.MIN_VOLUME_RATIO)
        if not spiking and state.volume_ratio < self.MIN_VOLUME_RATIO:
            return None

        # Direction from the candle body: a volume surge confirms whichever way
        # the bar closed, it does not pick the direction itself.
        last_candle = state.candles_1m[-1]
        is_bullish = last_candle.close >= last_candle.open

        return _emit(
            state,
            direction="long" if is_bullish else "short",
            signal_type="volume_spike",
            trigger_desc=(f"Trading volume just spiked to {state.volume_ratio:.1f}x normal "
                          "— a surge like this often kicks off a bigger move."),
            confidence=0.68,
            timeframe="30m",
            atr_target_multiple=1.5,
            reward_risk=1.0,
            extra_indicators=f"Vol {state.volume_ratio:.1f}x avg",
        )


class BollingerSqueezeAnalyzer:
    """Detects volatility compression (Bandwidth squeeze) preceding sharp directional expansion."""

    def analyze(self, state: CryptoState) -> CryptoSignal | None:
        if len(state.candles_1m) < 25 or state.bollinger_bandwidth <= 0:
            return None

        # Squeeze threshold: tight bandwidth (< 0.025 or 2.5% band width)
        if state.bollinger_bandwidth > 0.025:
            return None

        price = state.current_price
        # Breakout trigger if price crosses outer bands
        if price > state.bollinger_upper:
            direction = "long"
        elif price < state.bollinger_lower:
            direction = "short"
        else:
            return None

        return _emit(
            state, direction=direction, signal_type="bollinger_squeeze",
            trigger_desc=("Price had been coiled in a tight range and just broke out "
                          "— squeezes like this often lead to a bigger move."),
            confidence=0.70, timeframe="4h",
            atr_target_multiple=1.4, reward_risk=1.0,
            extra_indicators=f"Bandwidth {state.bollinger_bandwidth * 100:.2f}%",
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
