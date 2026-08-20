"""
The profit ladder: ratcheting the stop as return-on-margin crosses rungs.

Modelled on what the account already does by hand. A live SUI long at 25x sat
at +11.47% ROE with its stop moved to +7.53% ROE — above entry, so the worst
outcome had become a profit of Rs86 rather than a loss.
"""
import unittest
from datetime import UTC, datetime

from analysis.paper_trading import (
    NO_SLIPPAGE,
    FeeModel,
    ProfitLadder,
    Side,
    open_position,
)

T0 = datetime(2026, 8, 20, tzinfo=UTC)


class TestRungs(unittest.TestCase):
    def setUp(self):
        self.l = ProfitLadder(enabled=True)

    def test_nothing_locks_before_the_first_rung(self):
        self.assertIsNone(self.l.locked_roe(0.05))
        self.assertIsNone(self.l.locked_roe(0.19))

    def test_the_first_rung_locks_break_even_not_a_profit(self):
        """
        Locking a gain before there is room to give one back turns winners into
        scratches. The first rung only removes the loss.
        """
        self.assertEqual(self.l.locked_roe(0.20), 0.0)

    def test_every_rung_locks_less_than_it_triggers_on(self):
        """A rung locking what armed it would stop the trade out at that price."""
        for reached, locked in self.l.rungs:
            with self.subTest(reached=reached):
                self.assertLess(locked, reached)

    def test_rungs_ascend_so_the_lock_can_only_rise(self):
        reached = [r for r, _ in self.l.rungs]
        locked = [x for _, x in self.l.rungs]
        self.assertEqual(reached, sorted(reached))
        self.assertEqual(locked, sorted(locked))

    def test_it_reports_the_highest_rung_earned(self):
        self.assertEqual(self.l.locked_roe(1.10), 0.60)

    def test_beyond_the_last_rung_it_holds_the_last_lock(self):
        self.assertEqual(self.l.locked_roe(50.0), self.l.rungs[-1][1])

    def test_the_tight_preset_locks_earlier(self):
        tight = ProfitLadder.tight()
        self.assertIsNotNone(tight.locked_roe(0.15))
        self.assertIsNone(self.l.locked_roe(0.15))


class TestRatchet(unittest.TestCase):
    """Pinned against the real SUI position."""

    def _pos(self):
        f = FeeModel(taker_pct=0.0005, maintenance_margin_pct=0.0052)
        return open_position("suiusdt", Side.LONG, 0.6975, 1144.58, 25, f,
                             0.20, 1.0, T0, usdt_inr=102.0, lot_step=0.1,
                             slippage=NO_SLIPPAGE), f

    def test_the_real_position_reconciles(self):
        p, _ = self._pos()
        self.assertAlmostEqual(p.coin_qty, 402.2, places=1)
        self.assertAlmostEqual(p.margin, 1144.58, places=2)
        self.assertAlmostEqual(p.gross_pnl(0.7007), 131.28, delta=0.1)
        self.assertAlmostEqual(p.peak_roe(0.7007) * 100, 11.47, delta=0.1)

    def test_the_stop_crosses_entry_and_the_worst_case_turns_positive(self):
        """The whole point: after a rung, losing is off the table."""
        p, f = self._pos()
        lad = ProfitLadder(enabled=True)
        self.assertLess(p.gross_pnl(p.stop_price), 0)
        p.apply_ladder(0.7050, lad, f)
        self.assertGreater(p.stop_price, p.entry_price)
        self.assertGreater(p.gross_pnl(p.stop_price), 0)

    def test_break_even_covers_the_round_trip_not_just_entry(self):
        """Stopping out at entry still pays both fees."""
        p, f = self._pos()
        p.apply_ladder(0.7050, ProfitLadder(enabled=True), f)
        self.assertGreater(p.stop_price, p.entry_price * (1 + f.round_trip_pct() * 0.9))

    def test_the_stop_only_ever_climbs(self):
        p, f = self._pos()
        lad = ProfitLadder(enabled=True)
        seen = [p.stop_price]
        for px in (0.7050, 0.7120, 0.7020, 0.7260, 0.7000, 0.7500):
            p.apply_ladder(px, lad, f)
            seen.append(p.stop_price)
        for a, b in zip(seen, seen[1:]):
            self.assertGreaterEqual(b, a)

    def test_a_pullback_cannot_give_the_lock_back(self):
        p, f = self._pos()
        lad = ProfitLadder(enabled=True)
        p.apply_ladder(0.7260, lad, f)
        locked, stop = p.locked_roe, p.stop_price
        p.apply_ladder(0.6980, lad, f)
        self.assertEqual(p.locked_roe, locked)
        self.assertEqual(p.stop_price, stop)

    def test_it_does_nothing_while_disabled(self):
        p, f = self._pos()
        stop = p.stop_price
        self.assertFalse(p.apply_ladder(0.7500, ProfitLadder(), f))
        self.assertEqual(p.stop_price, stop)

    def test_a_losing_trade_is_left_alone(self):
        p, f = self._pos()
        self.assertFalse(p.apply_ladder(0.6900, ProfitLadder(enabled=True), f))
        self.assertIsNone(p.locked_roe)

    def test_the_locked_roe_is_actually_delivered_at_the_stop(self):
        """The rung is a promise about money, so check the money."""
        p, f = self._pos()
        p.apply_ladder(0.7260, ProfitLadder(enabled=True), f)
        realised = p.gross_pnl(p.stop_price) / p.margin
        self.assertGreaterEqual(realised, p.locked_roe)

    def test_shorts_ratchet_downward(self):
        f = FeeModel()
        p = open_position("ethusdt", Side.SHORT, 2000.0, 1000.0, 25, f, 0.20, 1.0,
                          T0, slippage=NO_SLIPPAGE)
        lad = ProfitLadder(enabled=True)
        start = p.stop_price
        p.apply_ladder(1960.0, lad, f)          # +50% ROE at 25x
        self.assertLess(p.stop_price, start)
        self.assertLess(p.stop_price, p.entry_price)
        self.assertGreater(p.gross_pnl(p.stop_price), 0)


class TestReachability(unittest.TestCase):
    """
    A ladder whose first rung sits at or above the target ROE is inert: the
    position closes at the target before any rung arms. With a fixed 20/20 the
    target is +20% ROE and the default ladder's first rung is also +20%.
    """

    def _cfg(self, rr, ladder):
        from analysis.paper_trading import CycleConfig
        return CycleConfig(leverage=25, stop_pct_of_margin=0.20,
                           reward_risk=rr, ladder=ladder)

    def test_the_default_ladder_is_inert_on_a_twenty_twenty(self):
        cfg = self._cfg(1.0, ProfitLadder(enabled=True))
        self.assertFalse(cfg.ladder_can_activate())
        self.assertEqual(cfg.ladder_rungs_reachable(), 0)

    def test_a_wider_target_opens_rungs_up(self):
        counts = [self._cfg(rr, ProfitLadder(enabled=True)).ladder_rungs_reachable()
                  for rr in (1.0, 3.0, 6.0, 15.0)]
        self.assertEqual(counts, sorted(counts))
        self.assertGreater(counts[-1], counts[0])

    def test_the_tight_preset_reaches_further_down(self):
        self.assertTrue(self._cfg(1.0, ProfitLadder.tight()).ladder_can_activate())

    def test_a_disabled_ladder_reports_no_rungs(self):
        self.assertEqual(self._cfg(6.0, ProfitLadder()).ladder_rungs_reachable(), 0)

    def test_the_sanity_report_carries_it(self):
        r = self._cfg(1.0, ProfitLadder(enabled=True)).sanity_report()
        self.assertIn("ladder_can_activate", r)
        self.assertIn("ladder_rungs_reachable", r)


class TestMeasuredAndDefaultedOff(unittest.TestCase):
    def test_it_ships_off(self):
        """
        Measured on synthetic candles at 25x it was worse at every target
        width: -2,234 off against -2,956 with it on at a 60% target, and the
        win rate roughly halved. Synthetic random walks contain no trend, and
        a ratchet's whole value is riding one — so this is grounds for leaving
        it off pending real data, not for concluding it does not work.
        """
        self.assertFalse(ProfitLadder().enabled)
        from config.settings import Settings
        self.assertFalse(Settings().paper_ladder_enabled)
        self.assertFalse(Settings().paper_ladder_tight)
