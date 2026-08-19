"""
Leverage scaled by confidence, bounded by volatility.

Leverage does not change the break-even move, and it does not change where the
stop sits relative to liquidation — at a 20% margin stop the stop is 20% of the
way to liquidation at every leverage. What it changes is the ABSOLUTE distance
to liquidation, and therefore whether an ordinary session can reach it.
"""
import unittest

from analysis.paper_trading import CycleConfig, LeverageConfig, SizingConfig


class TestConfidenceScaling(unittest.TestCase):
    def setUp(self):
        self.lc = LeverageConfig()

    def test_leverage_rises_with_confidence(self):
        levs = [self.lc.leverage_for(c) for c in (0.65, 0.70, 0.75, 0.80, 0.85)]
        self.assertEqual(levs, sorted(levs))
        self.assertAlmostEqual(levs[0], 10.0, places=6)
        self.assertAlmostEqual(levs[-1], 25.0, places=6)

    def test_it_never_goes_below_the_stated_floor(self):
        """10x was given as a hard minimum, so nothing may return less."""
        for c in (0.0, 0.30, 0.50, 0.64):
            self.assertGreaterEqual(self.lc.leverage_for(c), self.lc.floor_leverage)

    def test_it_never_exceeds_the_ceiling(self):
        for c in (0.85, 0.90, 0.99, 1.0):
            self.assertLessEqual(self.lc.leverage_for(c), self.lc.ceiling_leverage)


class TestVolatilityCap(unittest.TestCase):
    """The cap that matters: liquidation must survive an ordinary move."""

    def setUp(self):
        self.lc = LeverageConfig()

    def test_a_volatile_market_forces_leverage_down(self):
        calm = self.lc.leverage_for(0.85, atr_pct=0.005)
        wild = self.lc.leverage_for(0.85, atr_pct=0.040)
        self.assertGreater(calm, wild)
        self.assertAlmostEqual(wild, self.lc.floor_leverage, places=6)

    def test_gold_carries_more_leverage_than_ether_at_equal_confidence(self):
        """Not a preference — gold simply moves less, so liquidation is further in ATR."""
        gold = self.lc.leverage_for(0.85, atr_pct=0.010)
        eth = self.lc.leverage_for(0.85, atr_pct=0.020)
        self.assertGreater(gold, eth)

    def test_liquidation_stays_the_required_distance_away(self):
        for atr in (0.005, 0.010, 0.020, 0.030):
            lev = self.lc.leverage_for(0.85, atr_pct=atr)
            # (1/L - mm)/(1 - mm). The mm term is not negligible at high
            # leverage and omitting it overstates the room available.
            liq_move = (1.0 / lev - 0.0053) / (1 - 0.0053)
            with self.subTest(atr=atr):
                # The floor can override the cap, which is deliberate — the
                # decision to skip belongs to the signal gate, not to sizing.
                if lev > self.lc.floor_leverage + 1e-9:
                    self.assertGreaterEqual(liq_move / atr,
                                            self.lc.min_liquidation_atr - 0.01)

    def test_no_volatility_reading_leaves_the_confidence_value_alone(self):
        self.assertAlmostEqual(self.lc.leverage_for(0.85, atr_pct=None), 25.0, places=6)
        self.assertAlmostEqual(self.lc.leverage_for(0.85, atr_pct=0.0), 25.0, places=6)

    def test_a_hundred_x_on_ether_is_a_fifth_of_an_average_day(self):
        """Why the cap exists, stated as a number."""
        liq_at_100 = (1.0 / 100 - 0.0053) / (1 - 0.0053)
        self.assertLess(liq_at_100 / 0.020, 0.3)
        self.assertLess(self.lc.leverage_for(1.0, atr_pct=0.020), 100)

    def test_liquidation_distance_collapses_to_zero_at_one_over_mm(self):
        """A hard ceiling near 189x that 1/L alone does not reveal."""
        mm = 0.0053
        self.assertAlmostEqual((1.0 / (1 / mm) - mm) / (1 - mm), 0.0, places=9)


class TestExposureDoesNotCompound(unittest.TestCase):
    """
    Margin already scales with confidence. Scaling leverage as well makes
    notional grow with the product of the two, so the cap is not optional.
    """

    def setUp(self):
        self.cfg = CycleConfig(leverage=25.0, sizing=SizingConfig(),
                               leverage_scaling=LeverageConfig())

    def test_without_a_cap_exposure_would_grow_sevenfold(self):
        wallet = 3000.0
        low = (self.cfg.margin_for_signal(wallet, 0.65)
               * self.cfg.leverage_for_signal(0.65))
        high = (self.cfg.margin_for_signal(wallet, 0.85)
                * self.cfg.leverage_for_signal(0.85))
        self.assertGreater(high / low, 5.0)

    def test_the_cap_holds_notional_within_the_limit(self):
        wallet = 3000.0
        limit = self.cfg.leverage_scaling.notional_cap(wallet)
        for conf in (0.65, 0.70, 0.75, 0.80, 0.85, 0.95):
            lev = self.cfg.leverage_for_signal(conf)
            margin = self.cfg.cap_margin_to_notional(
                self.cfg.margin_for_signal(wallet, conf), lev, wallet)
            with self.subTest(conf=conf):
                self.assertLessEqual(margin * lev, limit + 1e-6)

    def test_a_fixed_leverage_config_is_left_untouched(self):
        fixed = CycleConfig(leverage=10.0, sizing=SizingConfig())
        self.assertEqual(fixed.leverage_for_signal(0.85), 10.0)
        self.assertEqual(fixed.cap_margin_to_notional(1500.0, 10.0, 3000.0), 1500.0)


class TestLeverageDoesNotChangeTheEdge(unittest.TestCase):
    """
    The point that keeps leverage honest: it scales gross and fee identically,
    so it can never turn a losing trade into a winning one.
    """

    def test_break_even_move_is_the_same_at_every_leverage(self):
        moves = {CycleConfig(leverage=L).break_even_move_pct() for L in (5, 10, 25, 50, 100)}
        self.assertEqual(len(moves), 1)

    def test_the_stop_creeps_closer_to_liquidation_as_leverage_rises(self):
        """
        I first asserted this ratio was constant. It is not: the maintenance
        margin shrinks the liquidation distance faster than 1/L, so the stop
        occupies a LARGER share of the room as leverage climbs — 21% at 10x,
        43% at 100x. Less margin for error exactly where there is least.
        """
        fractions = [CycleConfig(leverage=L, stop_pct_of_margin=0.20).stop_move_pct()
                     / CycleConfig(leverage=L).liquidation_move_pct()
                     for L in (10, 25, 50, 100)]
        self.assertEqual(fractions, sorted(fractions))
        self.assertAlmostEqual(fractions[0], 0.211, delta=0.005)
        self.assertAlmostEqual(fractions[-1], 0.426, delta=0.005)
