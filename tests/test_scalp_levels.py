"""
Level policy tests, anchored on the XRP signals from the live dashboard.

Those signals quoted entry $1.0023 with a target $1.0028 — a 0.050% move
against a 0.118% round trip. They were not marginal; they were guaranteed
losers that the analyzers had no way of noticing, because nothing in the old
`price +/- atr * k` formula ever consulted the cost of trading.
"""
import unittest
from datetime import UTC, datetime, timedelta

from analysis.crypto_signals import _emit
from analysis.crypto_state import CryptoState, OHLCVCandle
from analysis.scalp_levels import (
    NoTrade,
    ScalpConfig,
    ScalpLevels,
    round_to_tick,
    scalp_levels,
    tick_for_price,
)


class TestCostFloor(unittest.TestCase):
    def setUp(self):
        self.cfg = ScalpConfig()

    def test_cost_floor_is_fee_plus_spread_plus_slippage(self):
        self.assertAlmostEqual(self.cfg.cost_floor_pct, 0.00118 + 0.0002 + 0.0003, places=9)

    def test_minimum_target_is_a_multiple_of_the_floor(self):
        self.assertAlmostEqual(self.cfg.min_target_pct, self.cfg.cost_floor_pct * 2.0, places=9)

    def test_the_floor_exceeds_every_target_from_the_screenshot(self):
        """0.050%, 0.080% and 0.170% all sit under the 0.336% minimum."""
        for observed in (0.00050, 0.00080, 0.00170):
            self.assertLess(observed, self.cfg.min_target_pct)


class TestTickSize(unittest.TestCase):
    def test_tick_scales_with_price_magnitude(self):
        self.assertAlmostEqual(tick_for_price(1.0023), 0.0001, places=9)
        self.assertAlmostEqual(tick_for_price(1906.50), 0.1, places=9)
        self.assertAlmostEqual(tick_for_price(62000.0), 1.0, places=9)

    def test_a_fixed_four_decimals_is_wrong_at_both_ends(self):
        """The old round(price, 4) was coarse for XRP and pointless for BTC."""
        self.assertAlmostEqual(tick_for_price(1.0023) / 1.0023, 0.0001, delta=1e-5)
        self.assertLess(tick_for_price(62000.0) / 62000.0, 0.0001)

    def test_rounding_moves_away_from_entry(self):
        self.assertAlmostEqual(round_to_tick(1.00234, 0.0001, up=True), 1.0024, places=9)
        self.assertAlmostEqual(round_to_tick(1.00236, 0.0001, up=False), 1.0023, places=9)

    def test_zero_and_negative_prices_do_not_explode(self):
        self.assertEqual(tick_for_price(0.0), 0.0)
        self.assertEqual(tick_for_price(-5.0), 0.0)
        self.assertEqual(round_to_tick(1.5, 0.0, up=True), 1.5)


class TestScalpLevels(unittest.TestCase):
    def setUp(self):
        self.cfg = ScalpConfig()

    def test_the_screenshot_setup_is_refused(self):
        """XRP at 1.0023 with the ATR the flat feed produced: no trade."""
        got = scalp_levels(1.0023, True, 0.00019, self.cfg)
        self.assertIs(got, NoTrade.TOO_QUIET)

    def test_the_same_coin_trades_once_volatility_is_real(self):
        got = scalp_levels(1.0023, True, 0.005, self.cfg)
        self.assertIsInstance(got, ScalpLevels)
        self.assertGreater(got.target_pct, self.cfg.min_target_pct)
        self.assertGreater(got.edge_after_costs_pct, 0)

    def test_a_quiet_market_produces_no_trade_rather_than_a_small_one(self):
        """The inversion that matters: cost sets the floor, not volatility."""
        for atr in (0.0001, 0.0005, 0.001, 0.0015):
            with self.subTest(atr=atr):
                self.assertIs(scalp_levels(1906.5, True, atr, self.cfg), NoTrade.TOO_QUIET)

    def test_target_never_lands_below_the_minimum(self):
        for price in (0.5, 1.0023, 150.0, 1906.5, 62000.0):
            for atr in (0.002, 0.005, 0.01, 0.03):
                got = scalp_levels(price, True, atr, self.cfg)
                if isinstance(got, ScalpLevels):
                    with self.subTest(price=price, atr=atr):
                        self.assertGreaterEqual(got.target_pct, self.cfg.min_target_pct)
                        self.assertGreater(got.edge_after_costs_pct, 0)

    def test_edge_is_net_of_costs_not_the_raw_move(self):
        got = scalp_levels(1906.5, True, 0.005, self.cfg)
        self.assertAlmostEqual(got.edge_after_costs_pct,
                               got.target_pct - self.cfg.cost_floor_pct, places=9)
        self.assertLess(got.edge_after_costs_pct, got.target_pct)

    def test_long_and_short_are_mirror_images(self):
        lo = scalp_levels(1906.5, True, 0.005, self.cfg)
        sh = scalp_levels(1906.5, False, 0.005, self.cfg)
        self.assertGreater(lo.target, lo.entry)
        self.assertLess(lo.stop, lo.entry)
        self.assertLess(sh.target, sh.entry)
        self.assertGreater(sh.stop, sh.entry)
        self.assertAlmostEqual(lo.target_pct, sh.target_pct, delta=0.0002)

    def test_reward_risk_is_enforced_by_construction(self):
        got = scalp_levels(1906.5, True, 0.005, self.cfg, reward_risk=2.0)
        self.assertAlmostEqual(got.reward_risk, 2.0, delta=0.05)

    def test_funding_window_blocks_a_short_hold(self):
        got = scalp_levels(1906.5, True, 0.005, self.cfg, minutes_to_funding=5)
        self.assertIs(got, NoTrade.FUNDING_WINDOW)
        ok = scalp_levels(1906.5, True, 0.005, self.cfg, minutes_to_funding=60)
        self.assertIsInstance(ok, ScalpLevels)

    def test_levels_land_on_real_ticks(self):
        got = scalp_levels(1906.5, True, 0.005, self.cfg)
        for level in (got.target, got.stop):
            self.assertAlmostEqual(level / got.tick, round(level / got.tick), places=6)

    def test_zero_volatility_is_refused_not_defaulted(self):
        self.assertIs(scalp_levels(1906.5, True, 0.0, self.cfg), NoTrade.TOO_QUIET)


class TestAnalyzersRefuseFlatFeeds(unittest.TestCase):
    """
    The old analyzers fell back to `price * 0.015` when ATR was zero, which
    fabricated a 1.5% market out of a dead feed. That fallback is gone.
    """

    def _state(self, price, high, low):
        st = CryptoState(symbol="xrpusdt", base_asset="XRP", current_price=price)
        now = datetime.now(UTC)
        for _ in range(30):
            st.candles_1m.append(OHLCVCandle(price, high, low, price, 10.0, now))
        return st

    def test_flat_candles_produce_no_signal(self):
        st = self._state(1.0023, 1.0023, 1.0023)   # high == low, the REST feed
        st.atr_14 = 0.0
        self.assertIsNone(_emit(st, direction="long", signal_type="test",
                                trigger_desc="x", confidence=0.8, timeframe="1h"))

    def test_a_tiny_but_nonzero_atr_still_produces_no_signal(self):
        st = self._state(1.0023, 1.0025, 1.0021)
        st.atr_14 = 1.0023 * 0.00019      # what the dashboard actually had
        self.assertIsNone(_emit(st, direction="long", signal_type="test",
                                trigger_desc="x", confidence=0.9, timeframe="1h"))

    def test_high_confidence_cannot_override_the_cost_floor(self):
        """Confidence says how sure; it cannot make a move bigger than it is."""
        st = self._state(1.0023, 1.0025, 1.0021)
        st.atr_14 = 1.0023 * 0.0002
        for conf in (0.70, 0.85, 0.99):
            with self.subTest(conf=conf):
                self.assertIsNone(_emit(st, direction="long", signal_type="test",
                                        trigger_desc="x", confidence=conf,
                                        timeframe="1h"))

    def test_real_volatility_produces_a_viable_signal(self):
        st = self._state(1.0023, 1.0080, 0.9970)
        st.atr_14 = 1.0023 * 0.006
        sig = _emit(st, direction="long", signal_type="test",
                    trigger_desc="x", confidence=0.75, timeframe="1h")
        self.assertIsNotNone(sig)
        move = abs(sig.target_price - sig.current_price) / sig.current_price
        self.assertGreater(move, ScalpConfig().min_target_pct)
        self.assertGreater(sig.edge_pct, 0)


class TestRestPollsBuildRealCandles(unittest.IsolatedAsyncioTestCase):
    """
    Each REST poll used to append a candle with high == low == close, so true
    range was structurally zero and ATR decayed to nothing. Polls are now
    aggregated into a minute bar that has an actual range.
    """

    async def _feed(self, prices):
        from analysis.crypto_state_store import CryptoStateStore
        store = CryptoStateStore()
        base = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
        for i, px in enumerate(prices):
            await store.update_from_rest(
                "xrpusdt", px, max(prices), min(prices), 1_000_000.0, 0.0,
                base + timedelta(seconds=30 * i),
            )
        return await store.get("xrpusdt")

    async def test_polls_inside_one_minute_share_a_bar(self):
        st = await self._feed([1.0020, 1.0035])
        self.assertEqual(len(st.candles_1m), 1)
        bar = st.candles_1m[0]
        self.assertAlmostEqual(bar.open, 1.0020, places=6)
        self.assertAlmostEqual(bar.high, 1.0035, places=6)
        self.assertAlmostEqual(bar.low, 1.0020, places=6)
        self.assertAlmostEqual(bar.close, 1.0035, places=6)

    async def test_the_bar_has_a_real_range(self):
        st = await self._feed([1.0020, 1.0035])
        self.assertGreater(st.candles_1m[0].high - st.candles_1m[0].low, 0)

    async def test_a_new_minute_opens_a_new_bar_and_closes_the_last(self):
        st = await self._feed([1.0020, 1.0035, 1.0040])   # 0s, 30s, 60s
        self.assertEqual(len(st.candles_1m), 2)
        self.assertTrue(st.candles_1m[0].is_closed)
        self.assertFalse(st.candles_1m[-1].is_closed)

    async def test_atr_no_longer_collapses(self):
        prices = [1.0000 + (i % 7) * 0.0008 for i in range(60)]
        st = await self._feed(prices)
        self.assertGreater(st.atr_14, 0.0)
        # and it is now large enough to be a real input, not noise
        self.assertGreater(st.atr_14 / st.current_price, 0.0001)


class TestDashboardSharesTheCostModel(unittest.TestCase):
    """
    The page used to hardcode `2*0.0005*1.18*100` in JavaScript. Recalibrating
    fees against the ledger then left the dashboard on the stale rate, and it
    called signals viable that the engine would refuse.
    """

    def setUp(self):
        import scheduler.health as health
        self.html = health._HTML

    def test_no_placeholder_survives_rendering(self):
        self.assertNotIn("__BREAK_EVEN_PCT__", self.html)
        self.assertNotIn("__MIN_TARGET_PCT__", self.html)

    def test_injected_values_match_the_python_cost_model(self):
        import re
        cfg = ScalpConfig()
        be = re.search(r"const BREAK_EVEN_PCT = ([\d.]+);", self.html)
        mt = re.search(r"const MIN_TARGET_PCT = ([\d.]+);", self.html)
        self.assertIsNotNone(be)
        self.assertIsNotNone(mt)
        self.assertAlmostEqual(float(be.group(1)), cfg.round_trip_fee_pct * 100, places=4)
        self.assertAlmostEqual(float(mt.group(1)), cfg.min_target_pct * 100, places=4)

    def test_the_fee_arithmetic_is_no_longer_written_in_javascript(self):
        self.assertNotIn("2*0.0005*1.18", self.html)

    def test_the_page_gates_on_the_engine_bar_not_bare_break_even(self):
        self.assertIn("move >= MIN_TARGET_PCT", self.html)


class TestInstrumentSpecsAgainstRealTransactions(unittest.TestCase):
    """
    Pinned to four CoinDCX screenshots from 18 Aug 2026. The cost model used to
    assume one fee and one maintenance margin for every market; both vary.
    """

    def test_eth_close_fee_matches_the_ledger(self):
        """Fee Rs6.36 on a Rs10,776 close."""
        from analysis.instruments import spec_for
        notional = 0.055 * 1920.930 * 102.0
        self.assertAlmostEqual(notional * spec_for("ethusdt").effective_taker_pct,
                               6.36, delta=0.02)

    def test_gold_is_five_times_cheaper_than_ether(self):
        """Fee Rs1.73 on a Rs14,688 close — 0.0118%, not 0.0590%."""
        from analysis.instruments import spec_for
        eth, xau = spec_for("ethusdt"), spec_for("xauusdt")
        self.assertAlmostEqual(xau.effective_taker_pct * 100, 0.0118, places=4)
        self.assertAlmostEqual(eth.taker_pct / xau.taker_pct, 5.0, places=6)

    def test_gold_close_fee_matches_the_ledger(self):
        from analysis.instruments import spec_for
        notional = 14_687.73
        self.assertAlmostEqual(notional * spec_for("xauusdt").effective_taker_pct,
                               1.73, delta=0.02)

    def test_maintenance_margin_differs_by_market(self):
        """Back-solved from the two quoted liquidation prices."""
        from analysis.instruments import spec_for
        from analysis.paper_trading import Side, liquidation_price
        eth_liq = liquidation_price(1906.50, Side.LONG, 20,
                                    spec_for("ethusdt").maintenance_margin_pct)
        xau_liq = liquidation_price(4365.91, Side.LONG, 25,
                                    spec_for("xauusdt").maintenance_margin_pct)
        self.assertAlmostEqual(eth_liq, 1820.97, delta=0.5)
        self.assertAlmostEqual(xau_liq, 4234.93, delta=0.5)

    def test_the_usdt_inr_rate_reconciles_three_independent_ways(self):
        # gross P&L on the ETH close
        self.assertAlmostEqual(80.95 / (0.055 * (1920.930 - 1906.500)), 102.0, delta=0.1)
        # position size on the open XAU trade
        self.assertAlmostEqual(16_439.44 / (0.037 * 4355.97), 102.0, delta=0.1)
        # active P&L on the same position
        self.assertAlmostEqual(-37.50 / (0.037 * (4355.97 - 4365.91)), 102.0, delta=0.3)

    def test_unknown_markets_fall_back_to_the_expensive_side(self):
        """A cheap guess would admit trades that cannot pay for themselves."""
        from analysis.instruments import spec_for
        unknown = spec_for("dogeusdt")
        self.assertAlmostEqual(unknown.taker_pct, spec_for("ethusdt").taker_pct)
        self.assertGreater(unknown.taker_pct, spec_for("xauusdt").taker_pct)

    def test_base_asset_strips_every_quote_currency(self):
        from analysis.instruments import base_asset
        self.assertEqual(base_asset("ethusdt"), "ETH")
        self.assertEqual(base_asset("XAUUSDT"), "XAU")
        self.assertEqual(base_asset("btcinr"), "BTC")
        self.assertEqual(base_asset("solusdc"), "SOL")


class TestCostFrameIsPerMarket(unittest.TestCase):
    def test_gold_gets_a_far_lower_minimum_target(self):
        base = ScalpConfig()
        eth, xau = base.for_symbol("ethusdt"), base.for_symbol("xauusdt")
        self.assertAlmostEqual(eth.min_target_pct * 100, 0.336, places=3)
        self.assertLess(xau.min_target_pct, eth.min_target_pct)

    def test_a_gold_move_refused_as_crypto_is_accepted_as_gold(self):
        """The concrete consequence: same move, different verdict."""
        base = ScalpConfig()
        # Between the two floors: gold needs 0.0736%, crypto needs 0.168%.
        atr = 0.0012
        as_crypto = scalp_levels(4365.91, True, atr, base.for_symbol("ethusdt"),
                                 symbol="ethusdt")
        as_gold = scalp_levels(4365.91, True, atr, base.for_symbol("xauusdt"),
                               symbol="xauusdt")
        self.assertIs(as_crypto, NoTrade.TOO_QUIET)
        self.assertIsInstance(as_gold, ScalpLevels)

    def test_a_quiet_market_stretches_the_target_to_the_floor(self):
        """Above the floor but below the minimum, the target is raised — not shrunk."""
        base = ScalpConfig()
        got = scalp_levels(4365.91, True, 0.0020, base.for_symbol("ethusdt"),
                           symbol="ethusdt")
        self.assertAlmostEqual(got.target_pct, base.for_symbol("ethusdt").min_target_pct,
                               delta=0.00002)
        self.assertGreater(got.target_pct, 0.0020)

    def test_your_three_winning_trades_all_clear_the_floor(self):
        """
        ETH +0.757% at 6.4x cost, XAU +0.494% at 20.9x, XAU TP +1.243% at 52.7x.
        Every one is accepted; every dashboard signal was not.
        """
        base = ScalpConfig()
        for symbol, move in (("ethusdt", 0.007569), ("xauusdt", 0.004939),
                             ("xauusdt", 0.012430)):
            with self.subTest(symbol=symbol, move=move):
                self.assertGreater(move, base.for_symbol(symbol).min_target_pct)

    def test_the_dashboard_signals_do_not(self):
        crypto = ScalpConfig().for_symbol("xrpusdt")
        for move in (0.00050, 0.00080, 0.00170):
            with self.subTest(move=move):
                self.assertLess(move, crypto.min_target_pct)
