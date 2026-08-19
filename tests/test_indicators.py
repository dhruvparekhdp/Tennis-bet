"""
Indicator library: known values, boundary conditions, and the properties that
matter more than any single number.
"""
import math
import random
import unittest

from analysis import indicators as ind


def walk(n, seed, drift=0.0, vol=0.004, start=100.0):
    random.seed(seed)
    h, low, c, v = [], [], [], []
    p = start
    for _ in range(n):
        o = p
        p = o * (1 + drift + random.gauss(0, vol))
        w = abs(random.gauss(0, vol * 0.5)) * p
        h.append(max(o, p) + w)
        low.append(min(o, p) - w)
        c.append(p)
        v.append(random.uniform(80, 400))
    return h, low, c, v


class TestWarmUp(unittest.TestCase):
    """
    Every indicator returns None rather than a neutral-looking default.

    A 50.0 RSI computed from four bars is indistinguishable from a real 50.0,
    and that ambiguity is how a warm-up period becomes a signal.
    """

    def test_short_series_return_none(self):
        h, low, c, v = walk(5, 1)
        self.assertIsNone(ind.rsi(c))
        self.assertIsNone(ind.macd(c))
        self.assertIsNone(ind.stochastic(h, low, c))
        self.assertIsNone(ind.atr(h, low, c))
        self.assertIsNone(ind.bollinger(c))
        self.assertIsNone(ind.adx(h, low, c))
        self.assertIsNone(ind.supertrend(h, low, c))
        self.assertIsNone(ind.mfi(h, low, c, v))

    def test_empty_series_do_not_raise(self):
        self.assertIsNone(ind.sma([], 5))
        self.assertIsNone(ind.ema([], 5))
        self.assertIsNone(ind.roc([], 5))
        self.assertEqual(ind.swing_pivots([], 2, 2), [])


class TestKnownValues(unittest.TestCase):
    def test_sma_and_ema_on_a_flat_series(self):
        flat = [10.0] * 50
        self.assertAlmostEqual(ind.sma(flat, 20), 10.0)
        self.assertAlmostEqual(ind.ema(flat, 20), 10.0)

    def test_rsi_is_100_when_price_only_rises(self):
        rising = [100.0 + i for i in range(40)]
        self.assertAlmostEqual(ind.rsi(rising), 100.0, places=6)

    def test_rsi_is_0_when_price_only_falls(self):
        falling = [200.0 - i for i in range(40)]
        self.assertAlmostEqual(ind.rsi(falling), 0.0, places=6)

    def test_rsi_stays_in_bounds_on_noise(self):
        _, _, c, _ = walk(300, 4)
        r = ind.rsi(c)
        self.assertGreaterEqual(r, 0.0)
        self.assertLessEqual(r, 100.0)

    def test_bollinger_collapses_on_a_flat_series(self):
        upper, mid, lower, bw = ind.bollinger([10.0] * 40)
        self.assertAlmostEqual(upper, mid)
        self.assertAlmostEqual(lower, mid)
        self.assertAlmostEqual(bw, 0.0)

    def test_atr_is_zero_when_every_bar_is_identical(self):
        n = 40
        self.assertAlmostEqual(ind.atr([10.0] * n, [10.0] * n, [10.0] * n), 0.0)

    def test_macd_histogram_is_positive_in_an_uptrend(self):
        rising = [100.0 * (1.002 ** i) for i in range(120)]
        self.assertGreater(ind.macd(rising)[2], 0)

    def test_stochastic_pins_to_the_extremes(self):
        rising = [100.0 + i for i in range(60)]
        k, _ = ind.stochastic(rising, rising, rising)
        self.assertAlmostEqual(k, 100.0, places=6)


class TestTrend(unittest.TestCase):
    def test_adx_reports_a_trend_when_there_is_one(self):
        h, low, c, _ = walk(400, 2, drift=0.003, vol=0.002)
        a = ind.adx(h, low, c)
        self.assertTrue(a.is_trending)
        self.assertEqual(a.direction, 1)

    def test_adx_reports_no_trend_in_chop(self):
        """The value of ADX is that it says 'do not follow a trend here'."""
        h, low, c, _ = walk(400, 9, drift=0.0, vol=0.004)
        self.assertFalse(ind.adx(h, low, c).is_trending)

    def test_supertrend_follows_direction(self):
        h, low, c, _ = walk(300, 3, drift=0.003, vol=0.002)
        self.assertEqual(ind.supertrend(h, low, c)[1], 1)
        h, low, c, _ = walk(300, 3, drift=-0.003, vol=0.002)
        self.assertEqual(ind.supertrend(h, low, c)[1], -1)


class TestStructure(unittest.TestCase):
    def test_swing_pivots_find_real_turns(self):
        v = [1, 2, 3, 2, 1, 2, 5, 2, 1]
        highs = ind.swing_pivots(v, 2, 2, find_highs=True)
        self.assertIn(2, highs)   # the 3
        self.assertIn(6, highs)   # the 5

    def test_a_monotonic_series_has_no_pivots(self):
        self.assertEqual(ind.swing_pivots(list(range(20)), 2, 2, find_highs=True), [])

    def test_trend_structure_reads_higher_highs_and_lows(self):
        """Needs real pullbacks — vol comparable to drift, as a real trend has."""
        h, low, c, _ = walk(400, 6, drift=0.002, vol=0.004)
        self.assertEqual(ind.trend_structure(h, low), 1)

    def test_a_monotonic_rise_has_no_structure_to_read(self):
        """
        Surprising but correct: with no pullbacks there are no swing points, so
        there is nothing to compare. Returning 0 is honest; inferring "uptrend"
        from an average would be inventing structure that is not there.
        """
        rising = [100.0 + i for i in range(200)]
        self.assertEqual(ind.trend_structure(rising, rising), 0)

    def test_divergence_needs_the_oscillator_at_the_pivots(self):
        """
        Price makes a lower low while the oscillator makes a higher low.
        Constructed so that a naive "is the oscillator rising now" test would
        give the wrong answer — which is what the old detector did.
        """
        prices = [10, 8, 6, 8, 10, 9, 7, 5, 7, 9, 10, 11]
        osc = [40, 30, 20, 30, 40, 38, 32, 25, 35, 40, 42, 44]
        self.assertTrue(ind.divergence(prices, osc, bullish=True))

    def test_no_divergence_when_both_make_lower_lows(self):
        prices = [10, 8, 6, 8, 10, 9, 7, 4, 7, 9, 10, 11]
        osc = [40, 30, 20, 30, 40, 38, 32, 15, 35, 40, 42, 44]
        self.assertFalse(ind.divergence(prices, osc, bullish=True))

    def test_divergence_is_rare_on_pure_noise(self):
        """
        The old half-window check fired on 51% of bars of noise. Real
        pivot-based divergence has to be far rarer than that to be worth
        anything at all.
        """
        _, _, c, _ = walk(1200, 11)
        rs = ind.rsi_series(c)
        offset = len(c) - len(rs)
        fires = 0
        for end in range(60, len(rs)):
            if ind.divergence(c[offset:offset + end], rs[:end], bullish=True):
                fires += 1
        rate = fires / (len(rs) - 60)
        self.assertLess(rate, 0.35, f"divergence fired on {rate:.0%} of noise bars")


class TestVolatility(unittest.TestCase):
    def test_atr_pct_is_scale_free(self):
        """The same shape at two price levels must give the same ATR%."""
        h, low, c, _ = walk(200, 12, start=1.0)
        h2, low2, c2 = [x * 50_000 for x in h], [x * 50_000 for x in low], [x * 50_000 for x in c]
        self.assertAlmostEqual(ind.atr_pct(h, low, c), ind.atr_pct(h2, low2, c2), places=9)

    def test_squeeze_detects_compression(self):
        quiet = [100.0 + math.sin(i / 8) * 0.02 for i in range(120)]
        h = [x + 0.005 for x in quiet]
        low = [x - 0.005 for x in quiet]
        self.assertIsNotNone(ind.squeeze_on(h, low, quiet))

    def test_volatility_percentile_is_bounded(self):
        h, low, c, _ = walk(300, 13)
        p = ind.volatility_percentile(h, low, c)
        self.assertGreaterEqual(p, 0.0)
        self.assertLessEqual(p, 1.0)


class TestVolume(unittest.TestCase):
    def test_obv_rises_when_price_rises(self):
        c = [100.0 + i for i in range(30)]
        v = [100.0] * 30
        self.assertGreater(ind.obv(c, v), 0)

    def test_mfi_is_bounded(self):
        h, low, c, v = walk(200, 14)
        m = ind.mfi(h, low, c, v)
        self.assertGreaterEqual(m, 0.0)
        self.assertLessEqual(m, 100.0)

    def test_vwap_sits_inside_the_traded_range(self):
        h, low, c, v = walk(200, 15)
        w = ind.vwap(h, low, c, v)
        self.assertGreaterEqual(w, min(low))
        self.assertLessEqual(w, max(h))

    def test_zero_volume_does_not_divide_by_zero(self):
        h, low, c, _ = walk(50, 16)
        self.assertIsNone(ind.vwap(h, low, c, [0.0] * 50))
