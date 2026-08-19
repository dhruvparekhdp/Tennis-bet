"""
The high-conviction profile: fewer trades, more of each one kept.

Calibrated on the closed trades from the account. Every one of those was a
winner, so nothing here infers a win rate from them — that sample has no
losses in it and would flatter any strategy fitted to it. What IS taken from
the data is arithmetic that holds regardless of outcome: how much of a gross
profit survives the round trip.
"""
import unittest

from analysis.confluence import MIN_AGREEING_FAMILIES, ConvictionGate
from analysis.scalp_levels import ScalpConfig


class TestKeptFraction(unittest.TestCase):
    """
    kept = 1 - 1/(move / round_trip_cost).

    Pinned against the real trades, which match it to the percentage point.
    """

    def kept(self, x):
        return 1 - 1 / x

    def test_the_formula_reproduces_every_observed_trade(self):
        for move, rt, observed in (
            (0.803, 0.0236, 0.97),   # XAU long,  34.0x
            (1.878, 0.118, 0.94),    # ETH a,     15.9x
            (0.494, 0.0236, 0.95),   # XAU short, 20.9x
            (0.637, 0.118, 0.82),    # ETH b,      5.4x
            (0.177, 0.118, 0.34),    # ETH c,      1.5x — Rs38 fees on Rs58
        ):
            with self.subTest(move=move):
                self.assertAlmostEqual(self.kept(move / rt), observed, delta=0.02)

    def test_the_default_multiple_only_keeps_half(self):
        self.assertAlmostEqual(self.kept(ScalpConfig().min_edge_multiple), 0.50, places=6)

    def test_high_conviction_keeps_four_fifths(self):
        self.assertAlmostEqual(
            self.kept(ScalpConfig.high_conviction().min_edge_multiple), 0.80, places=6)

    def test_the_worst_observed_trade_falls_below_both_bars(self):
        """0.177% on ETH is 1.5x cost — under the default, far under the strict one."""
        x = 0.177 / 0.118
        self.assertLess(x, ScalpConfig().min_edge_multiple)
        self.assertLess(x, ScalpConfig.high_conviction().min_edge_multiple)


class TestTargetsRise(unittest.TestCase):
    def test_high_conviction_demands_a_bigger_move(self):
        d, h = ScalpConfig(), ScalpConfig.high_conviction()
        for sym in ("ethusdt", "xauusdt"):
            with self.subTest(sym=sym):
                self.assertGreater(h.for_symbol(sym).min_target_pct,
                                   d.for_symbol(sym).min_target_pct)

    def test_gold_still_gets_a_lower_bar_than_ether(self):
        """Cheaper fees mean a thinner move still clears — per-market, as before."""
        h = ScalpConfig.high_conviction()
        self.assertLess(h.for_symbol("xauusdt").min_target_pct,
                        h.for_symbol("ethusdt").min_target_pct)

    def test_the_thresholds_are_the_measured_ones(self):
        h = ScalpConfig.high_conviction()
        self.assertAlmostEqual(h.for_symbol("ethusdt").min_target_pct * 100, 0.840, places=3)
        self.assertAlmostEqual(h.for_symbol("xauusdt").min_target_pct * 100, 0.368, places=3)


class TestConvictionGate(unittest.TestCase):
    def test_default_matches_the_module_constant(self):
        g = ConvictionGate()
        self.assertEqual(g.min_agreeing, MIN_AGREEING_FAMILIES)
        self.assertIsNone(g.max_dissent)

    def test_high_conviction_forbids_dissent_rather_than_demanding_a_fourth_vote(self):
        """
        The measured point: no-dissent gave the same accuracy as a fourth vote
        while keeping four times the trades. A family with no opinion is not
        the same as a family arguing the other way.
        """
        g = ConvictionGate.high_conviction()
        self.assertEqual(g.max_dissent, 0)
        self.assertEqual(g.min_agreeing, MIN_AGREEING_FAMILIES)

    def test_strict_demands_both(self):
        g = ConvictionGate.strict()
        self.assertEqual(g.min_agreeing, 4)
        self.assertEqual(g.max_dissent, 0)


class TestGateBehaviourOnKnownData(unittest.TestCase):
    """Selectivity and direction, on series with an answer."""

    def _run(self, gate, seeds=range(40)):
        import random
        from datetime import UTC, datetime, timedelta

        from analysis.confluence import evaluate
        from analysis.crypto_state import OHLCVCandle

        t0 = datetime(2026, 8, 18, tzinfo=UTC)

        def feed(seed, drift):
            random.seed(seed)
            out, p = [], 100.0
            for i in range(240):
                o = p
                p = o * (1 + drift + random.gauss(0, 0.005))
                w = abs(random.gauss(0, 0.0025)) * p
                out.append(OHLCVCandle(o, max(o, p) + w, min(o, p) - w, p,
                                       random.uniform(80, 400), t0 + timedelta(minutes=i)))
            return out

        taken = correct = 0
        for drift, want in ((0.0015, "long"), (-0.0015, "short")):
            for s in seeds:
                v = evaluate(feed(s, drift), min_agreeing=gate.min_agreeing,
                             max_dissent=gate.max_dissent)
                if v.direction:
                    taken += 1
                    correct += v.direction == want
        return taken, correct

    def test_high_conviction_takes_fewer_trades_than_default(self):
        d, _ = self._run(ConvictionGate())
        h, _ = self._run(ConvictionGate.high_conviction())
        self.assertLess(h, d)
        self.assertGreater(h, 0, "a gate that never fires is not a strategy")

    def test_forbidding_dissent_does_not_cost_accuracy(self):
        d_t, d_c = self._run(ConvictionGate())
        h_t, h_c = self._run(ConvictionGate.high_conviction())
        self.assertGreaterEqual(h_c / h_t, d_c / d_t)

    def test_strict_is_the_most_selective_of_the_three(self):
        counts = [self._run(g)[0] for g in
                  (ConvictionGate(), ConvictionGate.high_conviction(), ConvictionGate.strict())]
        self.assertEqual(counts, sorted(counts, reverse=True))


class TestSurvivorshipIsNotEncoded(unittest.TestCase):
    def test_nothing_here_assumes_a_win_rate(self):
        """
        Every trade visible from the account was a winner. Fitting a win rate
        to that sample would be fitting to selection. The gate is built only
        from cost arithmetic and from agreement measured on synthetic data
        where losses are visible.
        """
        h = ScalpConfig.high_conviction()
        self.assertEqual(h.min_reward_risk, 1.0)      # symmetric, assumes nothing
        self.assertGreater(h.min_edge_multiple, ScalpConfig().min_edge_multiple)
