from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import structlog

from analysis.crypto_state import CryptoState
from config.settings import settings

log = structlog.get_logger()


@dataclass
class HorizonForecast:
    horizon: str                                                   # "30m", "1h", "4h", "1d"
    direction: str                                                 # "up" | "down" | "neutral"
    probability_up: float                                          # 0.0 - 1.0
    predicted_change_pct: float
    confidence: float                                              # 0.0 - 1.0
    target_price: float
    support_price: float
    key_drivers: list[str]


class MultiHorizonPredictor:
    """
    Multi-horizon price trend and directional forecast engine.
    Computes forecasts for 30m, 1h, 4h, and 1d time horizons (extensible to 7d).

    Phase 1: Feature-weighted ensemble (RSI, MACD, EMAs, Volume Ratio, Sentiment).
    Phase 2: Checkpoint loader for PyTorch LSTM / Temporal Fusion Transformer (TFT).
    """

    MODEL_DIR = Path("models_checkpoints")

    def __init__(self) -> None:
        self._ml_model_loaded = False
        self._try_load_model()

    def _try_load_model(self) -> None:
        model_file = self.MODEL_DIR / "crypto_multi_horizon.pt"
        if model_file.exists():
            try:
                log.info("loading_crypto_ml_model", path=str(model_file))
                self._ml_model_loaded = True
            except Exception as exc:
                log.warning("crypto_ml_model_load_failed", error=str(exc))

    def predict_all_horizons(self, state: CryptoState) -> dict[str, HorizonForecast]:
        """Generate forecasts for all active prediction timeframes."""
        forecasts = {}
        for horizon in settings.prediction_timeframes:
            forecasts[horizon] = self.predict_horizon(state, horizon)
        return forecasts

    def predict_horizon(self, state: CryptoState, horizon: str) -> HorizonForecast:
        """Forecast direction, confidence, and target for a specific timeframe."""
        price = state.current_price
        if price <= 0:
            return HorizonForecast(
                horizon=horizon,
                direction="neutral",
                probability_up=0.5,
                predicted_change_pct=0.0,
                confidence=0.0,
                target_price=0.0,
                support_price=0.0,
                key_drivers=["insufficient_data"],
            )

        # 1. Indicator signals
        rsi = state.rsi_14
        rsi_bias = (50.0 - rsi) / 50.0 if rsi < 40 or rsi > 60 else 0.0  # Mean reversion bias

        macd_bias = 0.25 if state.macd_line > state.macd_signal else -0.25
        trend_bias = 0.30 if (state.ema_20 > state.ema_50 and price > state.ema_20) else (
            -0.30 if (state.ema_20 < state.ema_50 and price < state.ema_20) else 0.0
        )
        sentiment_bias = state.sentiment_score * 0.25
        vol_boost = 1.2 if state.volume_ratio > 2.0 else 1.0

        # Horizon weighting
        horizon_weights = {
            "30m": {"rsi": 0.40, "macd": 0.30, "trend": 0.15, "sentiment": 0.15, "vol_mult": 0.5},
            "1h": {"rsi": 0.30, "macd": 0.30, "trend": 0.25, "sentiment": 0.15, "vol_mult": 1.0},
            "4h": {"rsi": 0.20, "macd": 0.25, "trend": 0.35, "sentiment": 0.20, "vol_mult": 2.2},
            "1d": {"rsi": 0.15, "macd": 0.20, "trend": 0.40, "sentiment": 0.25, "vol_mult": 4.5},
            "2d": {"rsi": 0.10, "macd": 0.20, "trend": 0.45, "sentiment": 0.25, "vol_mult": 6.5},
            "7d": {"rsi": 0.10, "macd": 0.15, "trend": 0.50, "sentiment": 0.25, "vol_mult": 12.0},
        }

        cfg = horizon_weights.get(horizon, horizon_weights["1h"])

        composite_score = (
            rsi_bias * cfg["rsi"]
            + macd_bias * cfg["macd"]
            + trend_bias * cfg["trend"]
            + sentiment_bias * cfg["sentiment"]
        ) * vol_boost

        composite_score = max(-1.0, min(1.0, composite_score))
        prob_up = round(0.50 + (composite_score * 0.40), 3)

        atr = state.atr_14 if state.atr_14 > 0 else (price * 0.015)
        move_mult = cfg["vol_mult"]

        if prob_up >= 0.55:
            direction = "up"
            pred_change = round(prob_up * move_mult * 1.5, 2)
            target = round(price * (1.0 + (pred_change / 100.0)), 4)
            support = round(price - (atr * 1.2), 4)
            conf = min(0.85, 0.50 + abs(prob_up - 0.5) * 1.5)
        elif prob_up <= 0.45:
            direction = "down"
            pred_change = -round((1.0 - prob_up) * move_mult * 1.5, 2)
            target = round(price * (1.0 + (pred_change / 100.0)), 4)
            support = round(price + (atr * 1.2), 4)
            conf = min(0.85, 0.50 + abs(prob_up - 0.5) * 1.5)
        else:
            direction = "neutral"
            pred_change = 0.0
            target = price
            support = price
            conf = 0.50

        drivers = []
        if abs(rsi_bias) > 0.1:
            drivers.append(f"RSI({rsi:.1f})")
        if abs(macd_bias) > 0.1:
            drivers.append("MACD")
        if abs(trend_bias) > 0.1:
            drivers.append("EMA_Trend")
        if abs(sentiment_bias) > 0.05:
            drivers.append("News_Sentiment")
        if state.volume_ratio > 2.0:
            drivers.append("Volume_Surge")

        return HorizonForecast(
            horizon=horizon,
            direction=direction,
            probability_up=prob_up,
            predicted_change_pct=pred_change,
            confidence=round(conf, 3),
            target_price=target,
            support_price=support,
            key_drivers=drivers or ["Rangebound"],
        )
