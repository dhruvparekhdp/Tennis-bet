"""
Unit tests for order flow, CVD (Cumulative Volume Delta), and absorption detection.
"""
from datetime import datetime, timezone, timedelta
import pytest
from analysis.crypto_state import OHLCVCandle
from analysis.orderflow import (
    bar_delta,
    cvd_series,
    cvd_delta_window,
    detect_absorption,
    cvd_alignment,
    compute_cvd_trend,
)


def _make_candle(open_: float, high: float, low: float, close: float, volume: float, taker_buy: float, is_closed: bool = True, idx: int = 0) -> OHLCVCandle:
    ts = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=idx)
    c = OHLCVCandle(
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        timestamp=ts,
        is_closed=is_closed,
    )
    c.taker_buy_volume = taker_buy
    return c


def test_bar_delta_calculation():
    # Zero taker buy
    c0 = _make_candle(100, 105, 95, 102, 100.0, 0.0)
    assert bar_delta(c0) == 0.0

    # Net positive delta: vol=100, buy=70 => sell=30 => delta = +40
    c1 = _make_candle(100, 105, 95, 102, 100.0, 70.0)
    assert bar_delta(c1) == 40.0

    # Net negative delta: vol=100, buy=30 => sell=70 => delta = -40
    c2 = _make_candle(100, 105, 95, 98, 100.0, 30.0)
    assert bar_delta(c2) == -40.0


def test_cvd_series():
    candles = [
        _make_candle(100, 105, 95, 101, 100, 60, is_closed=True, idx=0),   # delta = +20
        _make_candle(101, 106, 96, 102, 100, 70, is_closed=True, idx=1),   # delta = +40 (CVD=60)
        _make_candle(102, 107, 97, 100, 100, 20, is_closed=True, idx=2),   # delta = -60 (CVD=0)
        _make_candle(100, 101, 99, 100, 100, 80, is_closed=False, idx=3),  # unclosed, skipped
    ]
    series = cvd_series(candles)
    assert series == [20.0, 60.0, 0.0]


def test_cvd_delta_window():
    candles = [
        _make_candle(100, 105, 95, 101, 100, 60, idx=0),  # +20
        _make_candle(101, 106, 96, 102, 100, 70, idx=1),  # +40
        _make_candle(102, 107, 97, 100, 100, 20, idx=2),  # -60
    ]
    assert cvd_delta_window(candles, window=2) == -20.0  # +40 + (-60)
    assert cvd_delta_window(candles, window=10) == 0.0   # +20 + 40 - 60


def test_detect_absorption_bullish():
    # Create 20 bars. Bars 0-16 establish low of 95.0.
    # Bars 17-19 dip to a lower low of 93.0, but with heavy taker buying (net delta > 0).
    candles = []
    for i in range(17):
        candles.append(_make_candle(100, 105, 95, 100, 100, 50, idx=i))  # neutral delta

    # Last 3 bars: dip lower (low=93), but buyers aggressively cross spread (taker buy 80)
    for i in range(17, 20):
        candles.append(_make_candle(98, 100, 93, 97, 100, 80, idx=i))

    assert detect_absorption(candles, lookback=20) == "bullish_absorption"


def test_detect_absorption_bearish():
    # Bars 0-16 establish high of 105.0.
    # Bars 17-19 push to 108.0, but taker delta is strongly negative (buyers absorbed by sell wall).
    candles = []
    for i in range(17):
        candles.append(_make_candle(100, 105, 95, 100, 100, 50, idx=i))

    for i in range(17, 20):
        candles.append(_make_candle(104, 108, 103, 105, 100, 20, idx=i))  # delta = -60

    assert detect_absorption(candles, lookback=20) == "bearish_absorption"


def test_cvd_alignment():
    # Net positive delta
    candles_pos = [_make_candle(100, 105, 95, 101, 100, 70, idx=i) for i in range(5)]
    align_long, delta_long, _ = cvd_alignment(candles_pos, "long", window=5)
    assert align_long is True
    assert delta_long > 0

    align_short, delta_short, reason_short = cvd_alignment(candles_pos, "short", window=5)
    assert align_short is False
    assert "divergence" in reason_short


def test_compute_cvd_trend():
    candles = [_make_candle(100, 102, 98, 101, 100, 70, idx=i) for i in range(5)]
    assert compute_cvd_trend(candles) == "bullish_delta"

    candles_neg = [_make_candle(100, 102, 98, 99, 100, 30, idx=i) for i in range(5)]
    assert compute_cvd_trend(candles_neg) == "bearish_delta"
