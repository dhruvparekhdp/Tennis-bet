"""Tests for the leveraged futures maths and the backtest engine.

These pin down the behaviours that are easy to get subtly wrong and expensive
to get wrong silently: fees on notional, exact liquidation, pessimistic
resolution of ambiguous candles, and conservation of money.
"""
from __future__ import annotations

import random
import unittest
from datetime import UTC, datetime, timedelta

from analysis.backtest import BacktestEngine
from analysis.paper_trading import (
    CycleConfig,
    ExitReason,
    FeeModel,
    Side,
    close_position,
    liquidation_price,
    open_position,
    resolve_candle,
    stop_and_target,
)
from collectors.historical_klines import Candle

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def synthetic(n: int, seed: int, vol: float = 0.004, drift: float = 0.0,
              start: float = 100.0) -> list[Candle]:
    """Candles with a genuine high/low, unlike the flat ones REST polling makes."""
    random.seed(seed)
    out, p = [], start
    for i in range(n):
        o = p
        c = o * (1 + random.gauss(drift, vol))
        wick = abs(random.gauss(0, vol * 0.6))
        out.append(Candle(T0 + timedelta(minutes=i), o,
                          max(o, c) * (1 + wick), min(o, c) * (1 - wick),
                          c, random.uniform(80, 400)))
        p = c
    return out


class TestFuturesMaths(unittest.TestCase):
    def setUp(self):
        self.f = FeeModel()

    def test_fees_charged_on_notional_not_margin(self):
        """At 10x the round trip costs ~1.18% of margin. This is the trap."""
        margin = 1000.0
        for lev in (1, 5, 10, 20):
            rt = self.f.entry_fee(margin * lev) + self.f.exit_fee(margin * lev)
            self.assertAlmostEqual(rt / margin, 2 * self.f.effective_taker_pct * lev, places=9)

    def test_break_even_move_is_leverage_independent(self):
        """Leverage multiplies gain and fee equally, so it never changes the sign."""
        self.assertAlmostEqual(self.f.round_trip_pct(), 2 * 0.0005 * 1.18, places=9)

    def test_gst_is_included_in_the_effective_rate(self):
        """18% GST on brokerage is unavoidable, so it belongs in the fee, not a footnote."""
        self.assertAlmostEqual(self.f.effective_taker_pct, 0.0005 * 1.18, places=9)
        self.assertGreater(self.f.effective_taker_pct, self.f.taker_pct)

    def test_liquidation_long_and_short(self):
        lp = liquidation_price(100.0, Side.LONG, 10, 0.015)
        sp = liquidation_price(100.0, Side.SHORT, 10, 0.015)
        self.assertAlmostEqual(lp, 100 * (1 - 0.1) / (1 - 0.015), places=9)
        self.assertAlmostEqual(sp, 100 * (1 + 0.1) / (1 + 0.015), places=9)
        self.assertLess(lp, 100)
        self.assertGreater(sp, 100)

    def test_higher_leverage_moves_liquidation_closer(self):
        prev = 0.0
        for lev in (5, 10, 20, 50):
            dist = 100 - liquidation_price(100.0, Side.LONG, lev, 0.015)
            self.assertGreater(dist, 0)
            if prev:
                self.assertLess(dist, prev)
            prev = dist

    def test_stop_and_target_derive_from_margin_risk(self):
        """stop 20% of margin at 10x is a 2% price move; R:R 2.0 puts target at 4%."""
        stop, target = stop_and_target(100.0, Side.LONG, 10, 0.20, 2.0)
        self.assertAlmostEqual(stop, 98.0, places=9)
        self.assertAlmostEqual(target, 104.0, places=9)

    def test_short_stop_and_target_are_mirrored(self):
        stop, target = stop_and_target(100.0, Side.SHORT, 10, 0.20, 2.0)
        self.assertAlmostEqual(stop, 102.0, places=9)
        self.assertAlmostEqual(target, 96.0, places=9)

    def test_ambiguous_candle_books_the_loss(self):
        """Both stop and target inside one candle: we cannot know the path, so assume the worst."""
        p = open_position("X", Side.LONG, 100.0, 200.0, 10, self.f, 0.20, 2.0, T0)
        reason, _ = resolve_candle(p, high=105.0, low=97.0, close=101.0, ts=T0)
        self.assertIs(reason, ExitReason.STOP)

    def test_liquidation_takes_priority_over_stop(self):
        p = open_position("X", Side.LONG, 100.0, 200.0, 10, self.f, 0.20, 2.0, T0)
        reason, price = resolve_candle(p, high=100.0, low=85.0, close=86.0, ts=T0)
        self.assertIs(reason, ExitReason.LIQUIDATION)
        self.assertAlmostEqual(price, p.liq_price, places=9)

    def test_cannot_lose_more_than_margin(self):
        p = open_position("X", Side.LONG, 100.0, 200.0, 10, self.f, 0.20, 2.0, T0)
        t = close_position(p, 50.0, ExitReason.LIQUIDATION, T0, self.f, wallet_before=800.0)
        self.assertAlmostEqual(t.net_pnl, -200.0, places=9)
        self.assertAlmostEqual(t.wallet_after, 800.0, places=9)

    def test_short_profits_when_price_falls(self):
        p = open_position("X", Side.SHORT, 100.0, 200.0, 10, self.f, 0.20, 2.0, T0)
        reason, price = resolve_candle(p, high=101.0, low=95.5, close=96.0, ts=T0)
        self.assertIs(reason, ExitReason.TARGET)
        t = close_position(p, price, reason, T0, self.f, wallet_before=800.0)
        self.assertGreater(t.net_pnl, 0)

    def test_expiry_only_after_deadline(self):
        p = open_position("X", Side.LONG, 100.0, 200.0, 10, self.f, 0.20, 2.0, T0,
                          expires_at=T0 + timedelta(hours=4))
        self.assertIsNone(resolve_candle(p, 100.5, 99.5, 100.2, T0 + timedelta(hours=1)))
        reason, price = resolve_candle(p, 100.5, 99.5, 100.2, T0 + timedelta(hours=4))
        self.assertIs(reason, ExitReason.EXPIRY)
        self.assertEqual(price, 100.2)

    def test_winning_trade_matches_hand_calculation(self):
        p = open_position("X", Side.LONG, 100.0, 200.0, 10, self.f, 0.20, 2.0, T0)
        t = close_position(p, 104.0, ExitReason.TARGET, T0, self.f, wallet_before=800.0)
        eff = self.f.effective_taker_pct
        self.assertAlmostEqual(t.gross_pnl, (104 - 100) * 20.0, places=9)
        # closed instantly, so no funding accrues
        self.assertAlmostEqual(t.fees_paid, 2000 * eff + 104 * 20 * eff, places=9)
        self.assertAlmostEqual(t.wallet_after, 800 + 200 + t.net_pnl, places=9)

    def test_funding_accrues_with_time_held(self):
        """A position held for hours costs funding on top of the trading fees."""
        p = open_position("X", Side.LONG, 100.0, 200.0, 10, self.f, 0.20, 2.0, T0)
        quick = close_position(p, 100.0, ExitReason.EXPIRY, T0, self.f, 800.0)
        held = close_position(p, 100.0, ExitReason.EXPIRY,
                              T0 + timedelta(hours=24), self.f, 800.0)
        self.assertAlmostEqual(quick.funding_paid, 0.0, places=9)
        self.assertGreater(held.funding_paid, 0.0)
        self.assertGreater(held.fees_paid, quick.fees_paid)
        self.assertAlmostEqual(held.hours_held, 24.0, places=6)


class TestCalibrationAgainstRealTrades(unittest.TestCase):
    """
    Pins the cost model to figures observed on a real CoinDCX INR futures
    account on 18 Aug 2026. If CoinDCX changes its rates these will fail,
    which is exactly what should happen.
    """

    SIZE = 10681.44      # position notional, INR
    MARGIN = 533.31
    ENTRY = 1906.50
    OBSERVED_OPEN_FEE = 6.31
    OBSERVED_LIQ = 1820.97
    OBSERVED_FUNDING_16H = 1.43

    def setUp(self):
        self.f = FeeModel()

    def test_open_fee_matches_account(self):
        self.assertAlmostEqual(self.f.entry_fee(self.SIZE), self.OBSERVED_OPEN_FEE, delta=0.05)

    def test_liquidation_price_matches_account(self):
        lp = liquidation_price(self.ENTRY, Side.LONG, 20, self.f.maintenance_margin_pct)
        self.assertAlmostEqual(lp, self.OBSERVED_LIQ, delta=1.0)

    def test_funding_matches_account(self):
        self.assertAlmostEqual(self.f.funding_cost(self.SIZE, 16),
                               self.OBSERVED_FUNDING_16H, delta=0.25)

    def test_pnl_formula_matches_account(self):
        """qty x price move, converted at the implied USDT rate."""
        qty, ltp = 0.055, 1904.00
        usdt_inr = self.SIZE / (qty * self.ENTRY)
        pnl = qty * (ltp - self.ENTRY) * usdt_inr
        self.assertAlmostEqual(pnl, -14.03, delta=0.05)
        self.assertAlmostEqual(pnl / self.MARGIN * 100, -2.63, delta=0.05)


class TestConfigSanity(unittest.TestCase):
    def test_flags_targets_that_cannot_clear_fees(self):
        bad = CycleConfig(leverage=10, stop_pct_of_margin=0.20, reward_risk=0.05)
        self.assertFalse(bad.sanity_report()["target_clears_fees"])

    def test_accepts_a_workable_configuration(self):
        good = CycleConfig(leverage=10, stop_pct_of_margin=0.20, reward_risk=2.0)
        rep = good.sanity_report()
        self.assertTrue(rep["target_clears_fees"])
        self.assertTrue(rep["stop_inside_liquidation"])


class TestBacktestEngine(unittest.TestCase):
    def test_money_is_conserved(self):
        cfg = CycleConfig(starting_wallet=1000, leverage=10, min_confidence=0.60)
        r = BacktestEngine(cfg).run("BTCUSDT", synthetic(2000, seed=3))
        self.assertAlmostEqual(
            cfg.starting_wallet + sum(t.net_pnl for t in r.trades),
            r.final_wallet, places=6)

    def test_no_trade_opens_without_data(self):
        cfg = CycleConfig(starting_wallet=1000)
        r = BacktestEngine(cfg).run("BTCUSDT", [])
        self.assertEqual(r.ended_reason, "no_data")
        self.assertEqual(len(r.trades), 0)

    def test_engine_detects_edge_in_mean_reverting_market(self):
        """The analyzers are mean-reversion logic, so a reverting series should pay."""
        def reverting(n, seed, anchor=100.0, pull=0.02, vol=0.004):
            random.seed(seed)
            out, p = [], anchor
            for i in range(n):
                o = p
                c = o * (1 + (anchor - p) / anchor * pull + random.gauss(0, vol))
                w = abs(random.gauss(0, vol * 0.6))
                out.append(Candle(T0 + timedelta(minutes=i), o,
                                  max(o, c) * (1 + w), min(o, c) * (1 - w),
                                  c, random.uniform(80, 400)))
                p = c
            return out

        wins = 0
        for seed in (1, 7, 13, 29, 41):
            cfg = CycleConfig(starting_wallet=1000, leverage=10,
                              reward_risk=2.0, min_confidence=0.60)
            r = BacktestEngine(cfg).run("BTCUSDT", reverting(3000, seed))
            if r.net_pnl > 0:
                wins += 1
        self.assertGreaterEqual(wins, 3, "engine should profit in its home regime")

    def test_stop_is_respected_so_losses_stay_bounded(self):
        cfg = CycleConfig(starting_wallet=1000, leverage=10,
                          stop_pct_of_margin=0.20, min_confidence=0.60)
        r = BacktestEngine(cfg).run("BTCUSDT", synthetic(3000, seed=11, vol=0.006))
        for t in r.trades:
            if t.reason is ExitReason.STOP:
                # 20% stop plus round-trip fees, with a little slack for fill price
                self.assertGreater(t.return_on_margin, -0.35)


if __name__ == "__main__":
    unittest.main(verbosity=2)
