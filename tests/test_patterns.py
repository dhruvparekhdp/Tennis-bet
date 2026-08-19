"""Pattern detection: constructed bars with a known right answer."""
import unittest
from datetime import UTC, datetime

from analysis.crypto_state import OHLCVCandle
from analysis.patterns import (
    detect_all,
    double_top_bottom,
    engulfing,
    inside_bar,
    pin_bar,
    range_breakout,
    three_bar_reversal,
)

T = datetime(2026, 8, 18, tzinfo=UTC)


def bar(o, h, low, c, v=100.0):
    return OHLCVCandle(o, h, low, c, v, T)


class TestEngulfing(unittest.TestCase):
    def test_bullish(self):
        p = engulfing([bar(100, 101, 98, 99), bar(98.5, 103, 98, 102.5)])
        self.assertEqual(p.direction, 1)

    def test_bearish(self):
        p = engulfing([bar(99, 102, 98.5, 101), bar(101.5, 102, 97, 98)])
        self.assertEqual(p.direction, -1)

    def test_same_direction_is_not_engulfing(self):
        self.assertIsNone(engulfing([bar(98, 101, 97, 100), bar(100, 104, 99, 103)]))

    def test_engulfing_a_doji_does_not_count(self):
        """A body covering nothing is one bar next to nothing, not a reversal."""
        self.assertIsNone(engulfing([bar(100, 100.4, 99.6, 100), bar(99, 104, 98, 103)]))


class TestPinBar(unittest.TestCase):
    def test_long_lower_wick_is_bullish(self):
        self.assertEqual(pin_bar([bar(100, 100.5, 96, 100.2)]).direction, 1)

    def test_long_upper_wick_is_bearish(self):
        self.assertEqual(pin_bar([bar(100, 104, 99.5, 99.8)]).direction, -1)

    def test_a_wide_bar_with_two_wicks_is_not_a_pin(self):
        self.assertIsNone(pin_bar([bar(100, 104, 96, 100.5)]))

    def test_a_bar_with_no_body_is_refused(self):
        self.assertIsNone(pin_bar([bar(100, 104, 96, 100)]))


class TestInsideBar(unittest.TestCase):
    def test_detected_and_directionless(self):
        """
        Compression says nothing about direction. Returning 0 is the honest
        answer; guessing a side here is a common way to be confidently wrong.
        """
        p = inside_bar([bar(100, 105, 95, 101), bar(100, 102, 98, 101)])
        self.assertEqual(p.direction, 0)

    def test_a_wider_bar_is_not_inside(self):
        self.assertIsNone(inside_bar([bar(100, 102, 98, 101), bar(100, 105, 95, 101)]))


class TestThreeBarReversal(unittest.TestCase):
    def test_two_down_then_a_full_recovery(self):
        p = three_bar_reversal([bar(100, 100, 97, 98), bar(98, 98, 95, 96),
                                bar(96, 102, 96, 101)])
        self.assertEqual(p.direction, 1)

    def test_two_up_then_a_full_collapse(self):
        p = three_bar_reversal([bar(96, 99, 96, 98), bar(98, 101, 98, 100),
                                bar(100, 100, 94, 95)])
        self.assertEqual(p.direction, -1)


class TestRangeBreakout(unittest.TestCase):
    def _range_then(self, final):
        bars = [bar(100, 101, 99, 100) for _ in range(25)]
        bars.append(final)
        return bars

    def test_close_above_the_range(self):
        self.assertEqual(range_breakout(self._range_then(bar(100, 105, 100, 104))).direction, 1)

    def test_close_below_the_range(self):
        self.assertEqual(range_breakout(self._range_then(bar(100, 100, 95, 96))).direction, -1)

    def test_staying_inside_is_not_a_breakout(self):
        self.assertIsNone(range_breakout(self._range_then(bar(100, 101, 99, 100))))

    def test_the_atr_buffer_suppresses_a_one_tick_poke(self):
        """Without a buffer this fires on every bar that ticks past the range."""
        poke = self._range_then(bar(100, 101.01, 100, 101.005))
        self.assertIsNotNone(range_breakout(poke, buffer_atr=0.0))
        self.assertIsNone(range_breakout(poke, buffer_atr=1.0))


class TestDoubleTopBottom(unittest.TestCase):
    def test_two_highs_at_the_same_level(self):
        # The two equal highs must be the LAST two swing pivots; a later pivot
        # would be the one compared.
        highs = [99, 100, 101, 104, 101, 100, 101, 104, 101, 100, 99, 98]
        bars = [bar(h - 1, h, h - 2, h - 0.5) for h in highs]
        p = double_top_bottom(bars)
        self.assertIsNotNone(p)
        self.assertEqual(p.direction, -1)

    def test_levels_far_apart_are_not_a_double_top(self):
        highs = [99, 100, 101, 104, 101, 100, 101, 120, 101, 100, 99, 98]
        bars = [bar(h - 1, h, h - 2, h - 0.5) for h in highs]
        p = double_top_bottom(bars)
        self.assertTrue(p is None or p.direction != -1)


class TestDetectAll(unittest.TestCase):
    def test_returns_everything_present(self):
        # The bar before an engulfing must have a real body — engulfing a doji
        # is one bar next to nothing, and the detector correctly refuses it.
        bars = [bar(100, 101, 99, 99.2) for _ in range(25)]
        bars.append(bar(98.8, 103, 98, 102.5))
        names = {p.name for p in detect_all(bars)}
        self.assertIn("engulfing", names)

    def test_empty_input_is_safe(self):
        self.assertEqual(detect_all([]), [])
