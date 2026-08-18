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
    round_to_lot,
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

    def test_bch_close_fee_share_matches_ledger(self):
        """
        Real BCH close from the account: gross Rs63.42, fees Rs5.77, net Rs57.65.

        This pins the correction to an earlier over-broad claim that "fees are
        bigger than the profits". They are not, on trades sized like this one.
        The fee share of gross depends only on how far the target is, so the
        ledger fee implies a notional, and that notional implies the move.
        """
        gross, observed_fee = 63.42, 5.77
        implied_notional = observed_fee / self.f.round_trip_pct()
        self.assertAlmostEqual(implied_notional, 4890.0, delta=60.0)

        move_pct = gross / implied_notional
        self.assertGreater(move_pct, 0.012)          # a real 1.3% move
        self.assertLess(observed_fee / gross, 0.10)  # fees under a tenth of gross

    def test_fee_share_is_independent_of_leverage_and_size(self):
        """Leverage scales gross and fee alike; only target distance matters."""
        move = 0.0130
        shares = []
        for margin, lev in ((1000, 5), (1000, 10), (3000, 20)):
            notional = margin * lev
            shares.append(self.f.exit_fee(notional) * 2 / (notional * move))
        for s in shares[1:]:
            self.assertAlmostEqual(s, shares[0], places=9)
        self.assertAlmostEqual(shares[0], self.f.round_trip_pct() / move, places=9)


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


class TestPositionReview(unittest.TestCase):
    """
    The adaptive exit: re-checking whether the reason for a trade is still true.

    These pin the mechanics (shock detection, distance-to-stop, rate limiting)
    and the deliberate choice to default the feature OFF, which is backed by
    measurement rather than taste.
    """

    def setUp(self):
        from analysis.paper_trading import ReviewConfig
        self.rc = ReviewConfig()
        self.f = FeeModel()

    def test_defaults_to_off_because_it_measured_worse(self):
        from analysis.paper_trading import ReviewConfig
        self.assertFalse(ReviewConfig().enabled)

    def test_shock_detects_range_and_volume_spikes(self):
        from analysis.paper_trading import ReviewConfig, is_market_shock
        rc = ReviewConfig()
        self.assertTrue(is_market_shock(3.0, 1.0, 100, 100, rc))   # 3x ATR range
        self.assertTrue(is_market_shock(1.0, 1.0, 400, 100, rc))   # 4x volume
        self.assertFalse(is_market_shock(1.0, 1.0, 100, 100, rc))  # calm
        self.assertFalse(is_market_shock(2.0, 1.0, 100, 100, rc))  # below threshold

    def test_shock_ignores_missing_baselines(self):
        """No ATR or no volume history must not read as a shock."""
        from analysis.paper_trading import ReviewConfig, is_market_shock
        rc = ReviewConfig()
        self.assertFalse(is_market_shock(5.0, 0.0, 0, 0, rc))

    def test_adverse_fraction_measures_distance_to_stop(self):
        from analysis.paper_trading import adverse_fraction_of_stop
        p = open_position("X", Side.LONG, 100.0, 200.0, 10, self.f, 0.20, 2.0, T0)
        self.assertAlmostEqual(adverse_fraction_of_stop(p, 100.0), 0.0, places=6)
        self.assertAlmostEqual(adverse_fraction_of_stop(p, 99.0), 0.5, places=6)
        self.assertAlmostEqual(adverse_fraction_of_stop(p, 98.0), 1.0, places=6)
        # a favourable move is not adverse
        self.assertAlmostEqual(adverse_fraction_of_stop(p, 102.0), 0.0, places=6)

    def test_adverse_fraction_mirrors_for_shorts(self):
        from analysis.paper_trading import adverse_fraction_of_stop
        p = open_position("X", Side.SHORT, 100.0, 200.0, 10, self.f, 0.20, 2.0, T0)
        self.assertAlmostEqual(adverse_fraction_of_stop(p, 101.0), 0.5, places=6)
        self.assertAlmostEqual(adverse_fraction_of_stop(p, 99.0), 0.0, places=6)

    def test_presets_are_configured_as_documented(self):
        from analysis.paper_trading import ReviewConfig
        safe = ReviewConfig.shock_only()
        self.assertTrue(safe.enabled)
        self.assertFalse(safe.exit_on_direction_flip)
        self.assertIsNone(safe.exit_confidence_floor)
        loud = ReviewConfig.aggressive()
        self.assertTrue(loud.exit_on_direction_flip)
        self.assertIsNotNone(loud.exit_confidence_floor)

    def test_review_can_close_positions_when_enabled(self):
        """Aggressive preset should actually fire, proving the path is wired."""
        from analysis.paper_trading import ReviewConfig
        cfg = CycleConfig(starting_wallet=1000, leverage=10, min_confidence=0.60,
                          review=ReviewConfig.aggressive())
        r = BacktestEngine(cfg).run("BTCUSDT", synthetic(3000, seed=7))
        self.assertGreater(r.early_exits, 0)

    def test_disabled_review_never_fires(self):
        from analysis.paper_trading import ReviewConfig
        cfg = CycleConfig(starting_wallet=1000, leverage=10, min_confidence=0.60,
                          review=ReviewConfig(enabled=False))
        r = BacktestEngine(cfg).run("BTCUSDT", synthetic(3000, seed=7))
        self.assertEqual(r.early_exits, 0)


class TestConcurrency(unittest.TestCase):
    def test_never_stacks_two_positions_in_one_symbol(self):
        """A single symbol must never hold two positions at once."""
        cfg = CycleConfig(starting_wallet=1000, leverage=10,
                          min_confidence=0.60, max_concurrent=3)
        r = BacktestEngine(cfg).run("BTCUSDT", synthetic(3000, seed=9))
        # every trade must close before the next one opens
        ordered = sorted(r.trades, key=lambda t: t.position.opened_at)
        for a, b in zip(ordered, ordered[1:]):
            self.assertLessEqual(a.closed_at, b.position.opened_at)


class TestConfidenceScaledSizing(unittest.TestCase):
    """Bigger positions for stronger signals, with a cap on total exposure."""

    def setUp(self):
        from analysis.paper_trading import SizingConfig
        self.sc = SizingConfig()

    def test_produces_the_requested_tiers(self):
        """On a Rs3,000 wallet: ~500 weak, ~1000 decent, ~1500 strong."""
        self.assertAlmostEqual(self.sc.margin_for(3000, 0.65), 500, delta=15)
        self.assertAlmostEqual(self.sc.margin_for(3000, 0.75), 1000, delta=15)
        self.assertAlmostEqual(self.sc.margin_for(3000, 0.85), 1500, delta=15)

    def test_size_increases_monotonically_with_confidence(self):
        prev = 0.0
        for c in (0.65, 0.70, 0.75, 0.80, 0.85):
            m = self.sc.margin_for(3000, c)
            self.assertGreaterEqual(m, prev)
            prev = m

    def test_clamps_outside_the_confidence_band(self):
        self.assertAlmostEqual(self.sc.margin_for(3000, 0.30),
                               self.sc.margin_for(3000, 0.65), delta=1)
        self.assertAlmostEqual(self.sc.margin_for(3000, 0.99),
                               self.sc.margin_for(3000, 0.85), delta=1)

    def test_scales_with_wallet(self):
        """Sizing is proportional, so the same rules work at 1k and at 20k."""
        small = self.sc.margin_for(1000, 0.85)
        big = self.sc.margin_for(10000, 0.85)
        self.assertAlmostEqual(big / small, 10.0, delta=0.1)

    def test_total_exposure_cap_refuses_over_commitment(self):
        from analysis.paper_trading import SizingConfig
        capped = SizingConfig(max_total_exposure_pct=0.60)
        committed = 0.0
        opened = 0
        for _ in range(5):
            m = capped.margin_for(3000, 0.85, committed)
            if m <= 0:
                break
            committed += m
            opened += 1
        self.assertGreater(opened, 0)
        self.assertLessEqual(committed, 3000 * 0.60 + 1)

    def test_refuses_a_position_too_small_to_be_worth_the_fee(self):
        self.assertEqual(self.sc.margin_for(100, 0.85, already_committed=99.0), 0.0)


class TestTargetViabilityFilter(unittest.TestCase):
    """Signals whose target cannot pay for the round trip must be refused."""

    def setUp(self):
        self.cfg = CycleConfig(leverage=10)

    def test_rejects_the_live_dashboard_signals(self):
        """Every signal observed on the live dashboard was unviable."""
        for entry, target in [(1902, 1903), (1905, 1905.19),
                              (1905, 1905.19), (1904, 1902)]:
            self.assertFalse(self.cfg.is_target_viable(entry, target),
                             f"{entry}->{target} should be refused")

    def test_accepts_a_target_that_clears_the_hurdle(self):
        # 1.0% away, comfortably past the ~0.18% requirement
        self.assertTrue(self.cfg.is_target_viable(1900.0, 1919.0))

    def test_rejects_degenerate_prices(self):
        self.assertFalse(self.cfg.is_target_viable(1905.0, 1905.0))
        self.assertFalse(self.cfg.is_target_viable(0.0, 100.0))

    def test_threshold_follows_the_fee_model(self):
        """Raise fees and more targets become unviable."""
        pricey = CycleConfig(leverage=10, fees=FeeModel(taker_pct=0.005))
        self.assertTrue(self.cfg.is_target_viable(1900.0, 1906.0))
        self.assertFalse(pricey.is_target_viable(1900.0, 1906.0))

    def test_engine_counts_what_it_refuses(self):
        cfg = CycleConfig(starting_wallet=1000, leverage=10, min_confidence=0.60,
                          reward_risk=0.02)   # absurdly tight targets
        r = BacktestEngine(cfg).run("BTCUSDT", synthetic(2000, seed=4))
        self.assertGreater(r.signals_rejected_unviable, 0)
        self.assertEqual(len(r.trades), 0, "no unviable trade should ever open")


class TestQuantityAgainstRealTrade(unittest.TestCase):
    """
    Reconciles the whole position against the ETH screenshot, not just the fee.

    Account showed: Rs533 margin, 20x, entry 1906.50, LTP 1904.00, 0.055 ETH,
    position size Rs10,681.44, open fee Rs6.31, liquidation 1820.97,
    P&L -Rs14.03 (-2.63% ROE).
    """

    # The rate that reconciles quantity, fee and P&L simultaneously. It sits
    # well above spot USD/INR because CoinDCX's INR futures carry a premium.
    RATE = 102.005
    LOT = 0.001

    def setUp(self):
        self.f = FeeModel()
        self.pos = open_position(
            "ETHUSDT", Side.LONG, 1906.50, 533.0, 20, self.f, 0.20, 2.0,
            datetime(2026, 8, 18), usdt_inr=self.RATE, lot_step=self.LOT,
        )

    def test_coin_quantity_matches_account(self):
        self.assertAlmostEqual(self.pos.coin_qty, 0.055, places=6)

    def test_naive_notional_over_price_is_wrong(self):
        """The bug this replaced: INR notional / USDT price is 100x too big."""
        naive = self.pos.target_notional / self.pos.entry_price
        self.assertGreater(naive / self.pos.coin_qty, 90)

    def test_position_size_is_marked_to_ltp(self):
        self.assertAlmostEqual(self.pos.mark_notional(1904.00), 10681.44, delta=1.0)

    def test_open_fee_matches_account(self):
        self.assertAlmostEqual(self.pos.entry_fee, 6.31, delta=0.02)

    def test_pnl_and_roe_match_account(self):
        pnl = self.pos.gross_pnl(1904.00)
        self.assertAlmostEqual(pnl, -14.03, delta=0.02)
        self.assertAlmostEqual(pnl / self.pos.margin * 100, -2.63, delta=0.02)

    def test_lot_rounding_is_to_nearest_not_truncated(self):
        """Raw size is 0.054797; truncation would have shown 0.054, not 0.055."""
        raw = self.pos.target_notional / (self.RATE * self.pos.entry_price)
        self.assertLess(raw, 0.055)
        self.assertEqual(round_to_lot(raw, self.LOT), 0.055)


class TestScaleInScaleOut(unittest.TestCase):
    """Double-checks on increasing and decreasing an open position."""

    RATE, LOT = 102.005, 0.001

    def _pos(self, side=Side.LONG):
        return open_position(
            "ETHUSDT", side, 2000.00, 1000.0, 10, FeeModel(), 0.20, 2.0,
            datetime(2026, 8, 18), usdt_inr=self.RATE, lot_step=self.LOT,
        )

    # ---- increment -----------------------------------------------------
    def test_increase_averages_the_entry(self):
        p = self._pos()
        q0 = p.coin_qty
        p.increase(1000.0, 1900.00, FeeModel(), 0.20, 2.0)
        added = p.coin_qty - q0
        expected = (q0 * 2000.00 + added * 1900.00) / p.coin_qty
        self.assertAlmostEqual(p.entry_price, expected, places=6)
        self.assertLess(p.entry_price, 2000.00)   # averaged down
        self.assertGreater(p.entry_price, 1900.00)

    def test_increase_adds_margin_and_quantity(self):
        p = self._pos()
        q0, m0 = p.coin_qty, p.margin
        p.increase(500.0, 2000.00, FeeModel(), 0.20, 2.0)
        self.assertAlmostEqual(p.margin, m0 + 500.0, places=6)
        # Half the margin added, so about half the coins — but each fill is
        # snapped to a lot independently, so allow one lot of slack.
        self.assertAlmostEqual(p.coin_qty, q0 * 1.5, delta=self.LOT)

    def test_increase_charges_fee_only_on_the_added_notional(self):
        f = FeeModel()
        p = self._pos()
        before = p.entry_fee
        added_fee = p.increase(500.0, 2000.00, f, 0.20, 2.0)
        self.assertAlmostEqual(p.entry_fee, before + added_fee, places=9)
        # Half the original size added -> about half the original fee, within
        # the one-lot rounding on the added leg.
        one_lot_fee = f.entry_fee(self.LOT * 2000.00 * self.RATE)
        self.assertAlmostEqual(added_fee, before * 0.5, delta=one_lot_fee)

    def test_increase_moves_stop_target_and_liquidation_to_new_entry(self):
        f = FeeModel()
        p = self._pos()
        old_stop, old_liq = p.stop_price, p.liq_price
        p.increase(1000.0, 1900.00, f, 0.20, 2.0)
        self.assertLess(p.stop_price, old_stop)
        self.assertLess(p.liq_price, old_liq)
        self.assertAlmostEqual(
            p.liq_price,
            liquidation_price(p.entry_price, Side.LONG,
                              p.effective_leverage, f.maintenance_margin_pct),
            places=6,
        )

    def test_increase_below_one_lot_is_refused(self):
        p = self._pos()
        q0, m0 = p.coin_qty, p.margin
        self.assertEqual(p.increase(0.05, 2000.00, FeeModel(), 0.20, 2.0), 0.0)
        self.assertEqual(p.coin_qty, q0)
        self.assertEqual(p.margin, m0)

    # ---- decrement -----------------------------------------------------
    def test_reduce_leaves_leverage_and_liquidation_untouched(self):
        p = self._pos()
        lev, liq, entry = p.effective_leverage, p.liq_price, p.entry_price
        p.reduce(p.coin_qty / 2, 2100.00, FeeModel())
        self.assertAlmostEqual(p.effective_leverage, lev, places=6)
        self.assertAlmostEqual(p.liq_price, liq, places=6)
        self.assertAlmostEqual(p.entry_price, entry, places=9)

    def test_reduce_frees_margin_pro_rata(self):
        p = self._pos()
        m0, q0 = p.margin, p.coin_qty
        _, _, freed = p.reduce(q0 * 0.4, 2100.00, FeeModel())
        share = (q0 - p.coin_qty) / q0
        self.assertAlmostEqual(freed, m0 * share, places=6)
        self.assertAlmostEqual(p.margin, m0 - freed, places=6)

    def test_two_half_exits_equal_one_full_exit(self):
        """Scaling out in two steps must not create or destroy money."""
        f = FeeModel()
        whole = self._pos()
        g_whole = whole.gross_pnl(2100.00)
        f_whole = f.exit_fee(whole.mark_notional(2100.00))

        p = self._pos()
        half = round_to_lot(p.coin_qty / 2, self.LOT)
        g1, f1, _ = p.reduce(half, 2100.00, f)
        g2, f2, _ = p.reduce(p.coin_qty, 2100.00, f)
        self.assertAlmostEqual(g1 + g2, g_whole, places=6)
        self.assertAlmostEqual(f1 + f2, f_whole, places=6)
        self.assertAlmostEqual(p.coin_qty, 0.0, places=9)

    def test_reduce_short_realises_the_opposite_sign(self):
        f = FeeModel()
        long_, short = self._pos(Side.LONG), self._pos(Side.SHORT)
        g_long, _, _ = long_.reduce(long_.coin_qty, 2100.00, f)
        g_short, _, _ = short.reduce(short.coin_qty, 2100.00, f)
        self.assertGreater(g_long, 0)
        self.assertAlmostEqual(g_short, -g_long, places=6)

    def test_reduce_more_than_held_closes_the_position_only(self):
        p = self._pos()
        g, _, freed = p.reduce(p.coin_qty * 10, 2100.00, FeeModel())
        self.assertAlmostEqual(p.coin_qty, 0.0, places=9)
        self.assertAlmostEqual(p.margin, 0.0, places=6)
        self.assertAlmostEqual(freed, 1000.0, places=6)

    def test_round_trip_scale_in_then_full_out_conserves_money(self):
        f = FeeModel()
        p = self._pos()
        spent = p.entry_fee
        spent += p.increase(1000.0, 2000.00, f, 0.20, 2.0)
        g, fee, freed = p.reduce(p.coin_qty, 2000.00, f)
        self.assertAlmostEqual(g, 0.0, places=6)          # no price move
        self.assertAlmostEqual(freed, 2000.0, places=6)   # both margins back
        # Two opens on Rs10,000 of notional each, then one close on the
        # combined Rs20,000 — four units of the same fee, and nothing else.
        one_open = f.entry_fee(1000.0 * 10)
        self.assertAlmostEqual(spent + fee, 4 * one_open, delta=0.5)
