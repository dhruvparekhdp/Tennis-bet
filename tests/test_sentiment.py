"""
Sentiment adjusts confidence; it never fires a trade on its own.

Confidence sets position size, so these deltas move money. The tests exist
mostly to pin the bounds: sentiment must not be able to dominate the setup,
and it must not be able to present anything as a certainty.
"""
import unittest

from collectors.sentiment_feeds import (
    CROWDED_FUNDING_PER_8H,
    MAX_TOTAL_DELTA,
    FearGreed,
    adjust_confidence,
    fear_greed_adjustment,
    funding_adjustment,
)


class TestFearGreed(unittest.TestCase):
    def test_only_the_tails_count(self):
        self.assertTrue(FearGreed(12, "Extreme Fear").is_extreme)
        self.assertTrue(FearGreed(88, "Extreme Greed").is_extreme)
        for middling in (30, 48, 52, 70):
            self.assertFalse(FearGreed(middling, "").is_extreme)

    def test_the_middle_moves_nothing(self):
        self.assertEqual(fear_greed_adjustment(FearGreed(50, ""), "long").delta, 0.0)

    def test_missing_data_moves_nothing(self):
        self.assertEqual(fear_greed_adjustment(None, "long").delta, 0.0)

    def test_extreme_fear_favours_longs_and_penalises_shorts(self):
        fg = FearGreed(12, "Extreme Fear")
        self.assertGreater(fear_greed_adjustment(fg, "long").delta, 0)
        self.assertLess(fear_greed_adjustment(fg, "short").delta, 0)

    def test_extreme_greed_is_the_mirror(self):
        fg = FearGreed(88, "Extreme Greed")
        self.assertLess(fear_greed_adjustment(fg, "long").delta, 0)
        self.assertGreater(fear_greed_adjustment(fg, "short").delta, 0)

    def test_the_reason_is_human_readable(self):
        adj = fear_greed_adjustment(FearGreed(12, "Extreme Fear"), "long")
        self.assertIn("extreme fear", adj.reason)
        self.assertIn("12", adj.reason)


class TestFunding(unittest.TestCase):
    def test_normal_funding_moves_nothing(self):
        """The reconciled ledger sat near 0.0066% per 8h — that is not crowded."""
        self.assertEqual(funding_adjustment(0.0000655, "long").delta, 0.0)

    def test_missing_data_moves_nothing(self):
        self.assertEqual(funding_adjustment(None, "long").delta, 0.0)

    def test_crowded_longs_favour_a_short(self):
        crowded = CROWDED_FUNDING_PER_8H * 2
        self.assertGreater(funding_adjustment(crowded, "short").delta, 0)
        self.assertLess(funding_adjustment(crowded, "long").delta, 0)

    def test_crowded_shorts_are_the_mirror(self):
        crowded = -CROWDED_FUNDING_PER_8H * 2
        self.assertGreater(funding_adjustment(crowded, "long").delta, 0)
        self.assertLess(funding_adjustment(crowded, "short").delta, 0)

    def test_the_threshold_is_a_boundary_not_a_cliff_either_side_of_zero(self):
        just_under = CROWDED_FUNDING_PER_8H * 0.99
        self.assertEqual(funding_adjustment(just_under, "long").delta, 0.0)
        self.assertEqual(funding_adjustment(-just_under, "long").delta, 0.0)


class TestCombinedAdjustment(unittest.TestCase):
    def test_no_inputs_leaves_confidence_untouched(self):
        conf, reasons = adjust_confidence(0.75, "long")
        self.assertEqual(conf, 0.75)
        self.assertEqual(reasons, [])

    def test_agreeing_inputs_raise_confidence(self):
        conf, reasons = adjust_confidence(
            0.70, "long", FearGreed(12, "Extreme Fear"), -CROWDED_FUNDING_PER_8H * 2)
        self.assertGreater(conf, 0.70)
        self.assertEqual(len(reasons), 2)

    def test_disagreeing_inputs_cancel_out(self):
        """One for, one against — the setup is left to speak for itself."""
        conf, reasons = adjust_confidence(
            0.75, "long", FearGreed(12, "Extreme Fear"), CROWDED_FUNDING_PER_8H * 2)
        self.assertAlmostEqual(conf, 0.75, places=6)
        self.assertEqual(len(reasons), 2)   # both still explained

    def test_sentiment_cannot_dominate_the_setup(self):
        conf, _ = adjust_confidence(0.50, "long", FearGreed(5, ""), -0.05)
        self.assertLessEqual(conf - 0.50, MAX_TOTAL_DELTA + 1e-9)

    def test_nothing_is_ever_presented_as_a_certainty(self):
        conf, _ = adjust_confidence(0.94, "long", FearGreed(5, ""), -0.05)
        self.assertLessEqual(conf, 0.95)

    def test_confidence_never_goes_negative(self):
        conf, _ = adjust_confidence(0.02, "long", FearGreed(95, ""), 0.05)
        self.assertGreaterEqual(conf, 0.0)

    def test_it_can_push_a_signal_through_the_gate_but_only_just(self):
        """
        Sentiment changes size and admission at the margin — by design. The
        cap is what stops it turning a weak setup into a large position.
        """
        below, _ = adjust_confidence(0.66, "long")
        lifted, _ = adjust_confidence(0.66, "long", FearGreed(10, ""),
                                      -CROWDED_FUNDING_PER_8H * 2)
        self.assertLess(below, 0.70)
        self.assertGreaterEqual(lifted, 0.70)
        self.assertLess(lifted, 0.75)
