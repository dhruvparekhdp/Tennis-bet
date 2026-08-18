from __future__ import annotations

from datetime import datetime, timezone

from analysis.crypto_signal import CryptoSignal, compute_crypto_stake
from analysis.crypto_state import CryptoState


class RSIDivergenceAnalyzer:
    """Detects RSI divergence (momentum vs price discrepancies) indicating upcoming reversals."""

    def analyze(self, state: CryptoState) -> CryptoSignal | None:
        if len(state.candles_1m) < 20 or state.current_price <= 0:
            return None

        div = state.rsi_divergence(lookback=20)
        if not div:
            return None

        price = state.current_price
        atr = state.atr_14 if state.atr_14 > 0 else (price * 0.015)

        if div == "bullish":
            direction = "long"
            target_price = round(price + (atr * 2.5), 4)
            stop_loss = round(price - (atr * 1.2), 4)
            edge_pct = round(min(5.5, (target_price - price) / price * 100.0), 2)
            confidence = round(min(0.85, 0.65 + (35.0 - min(state.rsi_14, 35.0)) * 0.01), 2)
            trigger_desc = "Price dipped to a new low, but selling pressure is fading — often an early reversal signal."
        else:
            direction = "short"
            target_price = round(price - (atr * 2.5), 4)
            stop_loss = round(price + (atr * 1.2), 4)
            edge_pct = round(min(5.5, (price - target_price) / price * 100.0), 2)
            confidence = round(min(0.85, 0.65 + (max(state.rsi_14, 65.0) - 65.0) * 0.01), 2)
            trigger_desc = "Price hit a new high, but buying pressure is fading — often an early reversal signal."

        stake_pct = compute_crypto_stake(edge_pct, confidence)

        return CryptoSignal(
            symbol=state.symbol,
            signal_type="rsi_divergence",
            direction=direction,
            trigger_description=trigger_desc,
            confidence=confidence,
            current_price=price,
            target_price=target_price,
            stop_loss=stop_loss,
            edge_pct=edge_pct,
            stake_pct=stake_pct,
            timeframe="1h",
            sentiment_score=state.sentiment_score,
            indicators_summary=f"RSI: {state.rsi_14:.1f} | ATR: {atr:.2f} | 24h: {state.price_change_24h_pct:+.1f}%",
            timestamp=datetime.now(timezone.utc),
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

        if state.volume_24h_avg <= 0 or (avg_recent_vol / (state.volume_24h_avg / 288.0)) < self.MIN_VOLUME_RATIO:
            # Fallback: check 24h volume ratio
            if state.volume_ratio < self.MIN_VOLUME_RATIO:
                return None

        price = state.current_price
        atr = state.atr_14 if state.atr_14 > 0 else (price * 0.02)

        # Direction based on candle body & trend
        last_candle = state.candles_1m[-1]
        is_bullish = last_candle.close >= last_candle.open

        direction = "long" if is_bullish else "short"
        target_price = round(price + (atr * 2.0 if is_bullish else -atr * 2.0), 4)
        stop_loss = round(price - (atr * 1.0 if is_bullish else -atr * 1.0), 4)
        edge_pct = round(abs(target_price - price) / price * 100.0, 2)
        confidence = 0.68

        trigger_desc = (
            f"Trading volume just spiked to {state.volume_ratio:.1f}x normal — "
            f"a surge like this often kicks off a bigger move."
        )
        stake_pct = compute_crypto_stake(edge_pct, confidence)

        return CryptoSignal(
            symbol=state.symbol,
            signal_type="volume_spike",
            direction=direction,
            trigger_description=trigger_desc,
            confidence=confidence,
            current_price=price,
            target_price=target_price,
            stop_loss=stop_loss,
            edge_pct=edge_pct,
            stake_pct=stake_pct,
            timeframe="30m",
            sentiment_score=state.sentiment_score,
            indicators_summary=f"Vol: {state.volume_ratio:.1f}x avg | RSI: {state.rsi_14:.1f}",
            timestamp=datetime.now(timezone.utc),
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
            atr = state.atr_14 if state.atr_14 > 0 else (price * 0.015)
            target_price = round(price + (atr * 2.2), 4)
            stop_loss = round(state.bollinger_mid, 4)
        elif price < state.bollinger_lower:
            direction = "short"
            atr = state.atr_14 if state.atr_14 > 0 else (price * 0.015)
            target_price = round(price - (atr * 2.2), 4)
            stop_loss = round(state.bollinger_mid, 4)
        else:
            return None

        edge_pct = round(abs(target_price - price) / price * 100.0, 2)
        confidence = 0.70
        stake_pct = compute_crypto_stake(edge_pct, confidence)

        return CryptoSignal(
            symbol=state.symbol,
            signal_type="bollinger_squeeze",
            direction=direction,
            trigger_description=(
                "Price had been coiled in a tight range and just broke out — "
                "squeezes like this often lead to a bigger move."
            ),
            confidence=confidence,
            current_price=price,
            target_price=target_price,
            stop_loss=stop_loss,
            edge_pct=edge_pct,
            stake_pct=stake_pct,
            timeframe="4h",
            sentiment_score=state.sentiment_score,
            indicators_summary=f"Bandwidth: {state.bollinger_bandwidth*100:.2f}% | Mid: {state.bollinger_mid:.2f}",
            timestamp=datetime.now(timezone.utc),
        )


class SentimentShiftAnalyzer:
    """Fires when high-conviction FinBERT/CryptoPanic news sentiment diverges from price action."""

    def analyze(self, state: CryptoState) -> CryptoSignal | None:
        if abs(state.sentiment_score) < 0.35 or state.current_price <= 0:
            return None

        price = state.current_price
        atr = state.atr_14 if state.atr_14 > 0 else (price * 0.02)

        if state.sentiment_score >= 0.35 and state.rsi_14 < 60:
            direction = "long"
            target_price = round(price + (atr * 3.0), 4)
            stop_loss = round(price - (atr * 1.5), 4)
            confidence = round(min(0.82, 0.62 + state.sentiment_score * 0.20), 2)
            trigger_desc = (
                f"News coverage right now is strongly positive "
                f"({state.sentiment_news_count} recent articles)."
            )
        elif state.sentiment_score <= -0.35 and state.rsi_14 > 40:
            direction = "short"
            target_price = round(price - (atr * 3.0), 4)
            stop_loss = round(price + (atr * 1.5), 4)
            confidence = round(min(0.82, 0.62 + abs(state.sentiment_score) * 0.20), 2)
            trigger_desc = (
                f"News coverage right now is strongly negative "
                f"({state.sentiment_news_count} recent articles)."
            )
        else:
            return None

        edge_pct = round(abs(target_price - price) / price * 100.0, 2)
        stake_pct = compute_crypto_stake(edge_pct, confidence)

        return CryptoSignal(
            symbol=state.symbol,
            signal_type="sentiment_shift",
            direction=direction,
            trigger_description=trigger_desc,
            confidence=confidence,
            current_price=price,
            target_price=target_price,
            stop_loss=stop_loss,
            edge_pct=edge_pct,
            stake_pct=stake_pct,
            timeframe="1d",
            sentiment_score=state.sentiment_score,
            indicators_summary=f"Sentiment: {state.sentiment_score:+.2f} | News items: {state.sentiment_news_count}",
            timestamp=datetime.now(timezone.utc),
        )
