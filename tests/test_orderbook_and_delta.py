"""
Depth, sweeps, and the venue integration.

The trading half of this file is the one place in the repo where a bug costs
money rather than accuracy, so the tests are mostly about refusals: every rail,
proved to fire, and the signature construction pinned byte for byte against the
venue's own client.
"""
import unittest
from datetime import UTC, datetime, timedelta

from analysis.crypto_state import OHLCVCandle
from analysis.orderbook import (
    OrderBook,
    depth_within,
    imbalance,
    round_trip_execution_pct,
    slippage_pct,
    walk_book,
    wall_before,
)
from analysis.patterns import detect_all, liquidity_sweep
from analysis.scalp_levels import ScalpConfig
from collectors.delta_exchange import parse_candles, parse_orderbook, venue_symbol
from collectors.delta_trading import (
    ContractSpec,
    DeltaTradingClient,
    OrderRefused,
    contracts_for,
)

T0 = datetime(2026, 8, 20, tzinfo=UTC)


def book(bids=None, asks=None):
    return OrderBook(
        "ethusdt",
        bids or [(2521.9, 12), (2521.5, 30), (2521.0, 45), (2520.0, 80)],
        asks or [(2522.1, 10), (2522.5, 25), (2530.0, 400), (2540.0, 60)])


class TestBookBasics(unittest.TestCase):
    def test_a_crossed_book_is_a_bad_snapshot_not_a_tight_market(self):
        """Treating it as tight reports a negative spread and a free trade."""
        self.assertFalse(book(asks=[(2521.0, 5)]).is_usable)

    def test_an_empty_side_is_unusable(self):
        self.assertFalse(OrderBook("x", [], [(1.0, 1.0)]).is_usable)
        self.assertFalse(OrderBook("x", [(1.0, 1.0)], []).is_usable)

    def test_spread_is_measured_from_mid(self):
        b = book()
        self.assertAlmostEqual(b.mid, 2522.0, places=6)
        self.assertAlmostEqual(b.spread_pct, 0.2 / 2522.0, places=9)


class TestWalkingTheBook(unittest.TestCase):
    def test_a_small_order_fills_at_the_touch(self):
        avg, filled = walk_book(book().asks, 5)
        self.assertEqual(filled, 5)
        self.assertAlmostEqual(avg, 2522.1, places=6)

    def test_a_larger_order_pays_the_next_levels(self):
        avg, filled = walk_book(book().asks, 20)
        self.assertEqual(filled, 20)
        self.assertGreater(avg, 2522.1)

    def test_a_size_the_book_cannot_fill_reports_a_partial(self):
        _, filled = walk_book(book().asks, 10_000)
        self.assertLess(filled, 10_000)

    def test_slippage_is_none_when_the_size_cannot_fill(self):
        """
        Distinct from zero. "It costs nothing" and "this cannot be traded" must
        not be the same answer — one of them should stop the trade.
        """
        self.assertIsNone(slippage_pct(book(), True, 10_000))

    def test_slippage_is_always_signed_against_the_trader(self):
        for is_buy in (True, False):
            with self.subTest(buy=is_buy):
                self.assertGreater(slippage_pct(book(), is_buy, 5), 0)

    def test_the_round_trip_pays_both_sides(self):
        one = slippage_pct(book(), True, 5) + slippage_pct(book(), False, 5)
        self.assertAlmostEqual(round_trip_execution_pct(book(), 5), one, places=12)

    def test_bigger_size_costs_more(self):
        self.assertGreater(round_trip_execution_pct(book(), 40),
                           round_trip_execution_pct(book(), 5))


class TestWalls(unittest.TestCase):
    def test_a_block_between_entry_and_target_is_found(self):
        self.assertEqual(wall_before(book(), 2522.0, 2543.49, quantity=0.074), 2530.0)

    def test_a_wall_beyond_the_target_is_irrelevant(self):
        self.assertIsNone(wall_before(book(), 2522.0, 2525.0, quantity=0.074))

    def test_an_even_book_has_no_wall(self):
        """
        The bug this test exists for: sizing the threshold off the ORDER meant
        that at 0.074 ETH every resting level was three times the trade, so the
        best ask came back as a wall and every setup would have been refused.
        """
        flat = OrderBook("x", [(99, 10), (98, 10), (97, 10)],
                         [(101, 10), (102, 10), (103, 10)])
        self.assertIsNone(wall_before(flat, 100.0, 104.0, quantity=1))

    def test_a_level_smaller_than_the_order_cannot_stop_it(self):
        b = OrderBook("x", [(99, 1)], [(101, 1), (102, 1), (103, 900)])
        self.assertIsNone(wall_before(b, 100.0, 104.0, quantity=5000))

    def test_shorts_look_the_other_way(self):
        b = OrderBook("x", [(99, 5), (98, 900), (97, 5)], [(101, 5)])
        self.assertEqual(wall_before(b, 100.0, 96.0, quantity=1), 98)


class TestImbalance(unittest.TestCase):
    def test_more_bids_reads_positive(self):
        b = OrderBook("x", [(99.9, 100), (99.8, 100)], [(100.1, 5)])
        self.assertGreater(imbalance(b), 0)

    def test_it_stays_within_minus_one_and_one(self):
        for bid in (1, 50, 5000):
            b = OrderBook("x", [(99.9, bid)], [(100.1, 7)])
            with self.subTest(bid=bid):
                self.assertGreaterEqual(imbalance(b), -1.0)
                self.assertLessEqual(imbalance(b), 1.0)

    def test_depth_respects_the_distance_limit(self):
        b = OrderBook("x", [(99.99, 5), (90.0, 900)], [(100.01, 5)])
        self.assertEqual(depth_within(b, 0.002, True), 5)


class TestCostFrameIsRepriced(unittest.TestCase):
    def test_a_measurement_replaces_both_guesses(self):
        cfg = ScalpConfig().for_symbol("ethusdt")
        measured = cfg.with_measured_execution(0.00008)
        self.assertEqual(measured.slippage_buffer_pct, 0.0)
        self.assertLess(measured.cost_floor_pct, cfg.cost_floor_pct)

    def test_the_brokerage_survives_the_reprice(self):
        """Execution is measurable; the fee is a fact and stays put."""
        cfg = ScalpConfig().for_symbol("ethusdt")
        self.assertEqual(cfg.with_measured_execution(0.0).round_trip_fee_pct,
                         cfg.round_trip_fee_pct)

    def test_a_wide_book_makes_the_floor_worse_not_better(self):
        cfg = ScalpConfig().for_symbol("ethusdt")
        self.assertGreater(cfg.with_measured_execution(0.004).cost_floor_pct,
                           cfg.cost_floor_pct)


class TestLiquiditySweep(unittest.TestCase):
    def _range_with_pivot_high(self):
        cs = [OHLCVCandle(100.0, 100.4, 99.7, 100.05, 100.0,
                          T0 + timedelta(minutes=i)) for i in range(26)]
        cs[9] = OHLCVCandle(100.1, 101.0, 99.9, 100.2, 100.0, T0 + timedelta(minutes=9))
        return cs

    def test_a_sweep_of_highs_is_a_short(self):
        cs = self._range_with_pivot_high()
        cs.append(OHLCVCandle(100.4, 101.6, 100.2, 100.35, 400.0,
                              T0 + timedelta(minutes=26)))
        got = liquidity_sweep(cs)
        self.assertIsNotNone(got)
        self.assertEqual(got.direction, -1)

    def test_a_break_that_holds_is_not_a_sweep(self):
        """The whole distinction. Closing beyond the level is a breakout."""
        cs = self._range_with_pivot_high()
        cs.append(OHLCVCandle(100.4, 101.6, 100.2, 101.5, 400.0,
                              T0 + timedelta(minutes=26)))
        self.assertIsNone(liquidity_sweep(cs))

    def test_a_sweep_of_lows_is_a_long(self):
        cs = [OHLCVCandle(100.0, 100.3, 99.6, 100.05, 100.0,
                          T0 + timedelta(minutes=i)) for i in range(26)]
        cs[9] = OHLCVCandle(99.9, 100.1, 99.0, 99.8, 100.0, T0 + timedelta(minutes=9))
        cs.append(OHLCVCandle(99.6, 99.8, 98.4, 99.75, 400.0,
                              T0 + timedelta(minutes=26)))
        got = liquidity_sweep(cs)
        self.assertIsNotNone(got)
        self.assertEqual(got.direction, 1)

    def test_a_rally_that_merely_stalls_is_not_a_sweep(self):
        """
        This bar clears the pivot high and closes below it, like a sweep, but
        most of its range is the move UP — it ran 1.85 from the low and gave
        back 0.6. That is a rally running out of steam, not stops being taken
        and the level defended, and the rejection test separates them.

        The low is kept above the pivot LOW on purpose. An earlier version of
        this fixture reached down to 99.0, which swept the lows at 99.7 and
        closed strongly back above them — a textbook sweep in the other
        direction, correctly detected, and a fixture testing the opposite of
        what it claimed.
        """
        cs = self._range_with_pivot_high()
        cs.append(OHLCVCandle(100.0, 101.6, 99.75, 100.99, 400.0,
                              T0 + timedelta(minutes=26)))
        self.assertIsNone(liquidity_sweep(cs))

    def test_it_needs_enough_history(self):
        self.assertIsNone(liquidity_sweep(self._range_with_pivot_high()[:8]))

    def test_it_joins_the_pattern_family(self):
        cs = self._range_with_pivot_high()
        cs.append(OHLCVCandle(100.4, 101.6, 100.2, 100.35, 400.0,
                              T0 + timedelta(minutes=26)))
        self.assertIn("liquidity_sweep", [p.name for p in detect_all(cs, 0.4)])


class TestDeltaParsing(unittest.TestCase):
    def test_symbols_map_and_unlisted_ones_return_none(self):
        self.assertEqual(venue_symbol("ethusdt"), "ETHUSD")
        # Gold is listed here as a tokenised perpetual, under a name string
        # surgery would never produce.
        self.assertEqual(venue_symbol("xauusdt"), "XAUTUSD")
        self.assertIsNone(venue_symbol("nosuchusdt"))

    def test_candles_parse_from_the_result_envelope(self):
        got = parse_candles({"result": [
            {"time": 1755000000, "open": 1, "high": 2, "low": 0.5,
             "close": 1.5, "volume": 9}]})
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["volume"], 9.0)

    def test_seconds_and_milliseconds_agree(self):
        a = parse_candles([[1755000000, 1, 2, 0.5, 1.5, 9]])
        b = parse_candles([[1755000000000, 1, 2, 0.5, 1.5, 9]])
        self.assertEqual(a[0]["timestamp"], b[0]["timestamp"])

    def test_junk_is_skipped_not_raised(self):
        for junk in ({"error": "no"}, None, "text", [{"open": 1}], [[1, 2]]):
            with self.subTest(junk=junk):
                self.assertEqual(parse_candles(junk), [])

    def test_the_book_comes_back_sorted_the_way_the_walk_needs(self):
        """
        Bids must descend and asks ascend. A venue returning them the other way
        round would produce fill prices that flatter every trade rather than
        failing loudly, so the parser sorts rather than trusting.
        """
        b = parse_orderbook("ethusdt", {"result": {
            "buy": [{"price": "2520", "size": "5"}, {"price": "2521.9", "size": "12"}],
            "sell": [{"price": "2530", "size": "8"}, {"price": "2522.1", "size": "10"}]}})
        self.assertEqual([p for p, _ in b.bids], [2521.9, 2520.0])
        self.assertEqual([p for p, _ in b.asks], [2522.1, 2530.0])

    def test_a_one_sided_book_is_refused(self):
        self.assertIsNone(parse_orderbook("x", {"result": {"buy": [], "sell": []}}))


class TestSigningMatchesTheVenueClient(unittest.TestCase):
    """
    Pinned against Delta's own python-rest-client. Every part is load-bearing
    and none of it is guessable from prose.
    """

    def setUp(self):
        self.c = DeltaTradingClient("key", "secret")

    def test_the_query_string_keeps_insertion_order_and_url_encodes(self):
        """Sorting looks tidier and signs one request while sending another."""
        self.assertEqual(self.c._query_string({"b": "x y", "a": "1"}), "?b=x+y&a=1")

    def test_no_params_means_an_empty_string_not_a_bare_question_mark(self):
        self.assertEqual(self.c._query_string(None), "")
        self.assertEqual(self.c._query_string({}), "")

    def test_the_body_is_compact_json(self):
        """Default separators insert ", " and hash a body nobody sent."""
        self.assertEqual(self.c._body_string({"a": 1, "b": "2"}), '{"a":1,"b":"2"}')
        self.assertEqual(self.c._body_string(None), "")

    def test_the_signature_is_hmac_sha256_of_method_ts_path_query_body(self):
        import hashlib
        import hmac
        ts, sig = self.c._sign("GET", "/v2/positions", "?product_id=27", "")
        expected = hmac.new(b"secret",
                            f"GET{ts}/v2/positions?product_id=27".encode(),
                            hashlib.sha256).hexdigest()
        self.assertEqual(sig, expected)

    def test_the_timestamp_is_signed_and_returned_together(self):
        """Generating it twice gives two seconds under load and fails every call."""
        ts, _ = self.c._sign("GET", "/x", "", "")
        self.assertTrue(ts.isdigit())


class TestSizing(unittest.TestCase):
    SPEC = ContractSpec(1282, "ETHUSD", 0.01, 0.1)

    def test_rs300_at_10x_buys_one_eth_contract(self):
        self.assertEqual(contracts_for(300, 10, 2522.0, self.SPEC, 102.0), 1)

    def test_it_rounds_down_never_up(self):
        """
        Rounding 1.4 up to 2 is 40% more risk than the caller asked for, taken
        silently, on every trade.
        """
        self.assertEqual(contracts_for(400, 10, 2522.0, self.SPEC, 102.0), 1)

    def test_too_little_margin_buys_nothing(self):
        self.assertEqual(contracts_for(50, 1, 2522.0, self.SPEC, 102.0), 0)

    def test_nonsense_inputs_return_zero_rather_than_raising(self):
        for args in ((0, 10, 2522.0), (300, 0, 2522.0), (300, 10, 0)):
            with self.subTest(args=args):
                self.assertEqual(contracts_for(*args, self.SPEC, 102.0), 0)


class TestTheRails(unittest.TestCase):
    def client(self, **kw):
        kw.setdefault("enabled", True)
        kw.setdefault("dry_run", True)
        kw.setdefault("max_notional_inr", 3000.0)
        c = DeltaTradingClient("key", "secret", **kw)
        c._specs["ethusdt"] = ContractSpec(1282, "ETHUSD", 0.01, 0.1)
        return c

    def test_disabled_refuses(self):
        with self.assertRaises(OrderRefused):
            self.client(enabled=False).build_entry("ethusdt", True, 2522.0, 1, 2511.25)

    def test_missing_credentials_refuse_even_when_enabled(self):
        c = DeltaTradingClient("", "", enabled=True, dry_run=False)
        c._specs["ethusdt"] = ContractSpec(1282, "ETHUSD", 0.01, 0.1)
        with self.assertRaises(OrderRefused):
            c.build_entry("ethusdt", True, 2522.0, 1, 2511.25)

    def test_an_entry_with_no_stop_is_refused(self):
        """At 10x an unprotected position is a way to lose it all to one wick."""
        for stop in (None, 0.0):
            with self.subTest(stop=stop):
                with self.assertRaises(OrderRefused):
                    self.client().build_entry("ethusdt", True, 2522.0, 1, stop)

    def test_over_the_cap_is_refused_not_resized(self):
        """Resizing hides that the caller asked for something it should not."""
        with self.assertRaises(OrderRefused) as ctx:
            self.client().build_entry("ethusdt", True, 2522.0, 5, 2511.25)
        self.assertIn("cap", str(ctx.exception))

    def test_less_than_one_contract_is_refused(self):
        with self.assertRaises(OrderRefused):
            self.client().build_entry("ethusdt", True, 2522.0, 0, 2511.25)

    def test_an_unlisted_symbol_is_refused(self):
        with self.assertRaises(OrderRefused):
            self.client().build_entry("xauusdt", True, 2522.0, 1, 2511.25)


class TestOrderBodies(unittest.TestCase):
    def setUp(self):
        self.c = DeltaTradingClient("key", "secret", enabled=True, dry_run=True,
                                    max_notional_inr=3000.0)
        self.c._specs["ethusdt"] = ContractSpec(1282, "ETHUSD", 0.01, 0.1)

    def test_the_entry_is_a_market_ioc_on_the_product_id(self):
        body = self.c.build_entry("ethusdt", True, 2522.0, 1, 2511.25)
        self.assertEqual(body["product_id"], 1282)
        self.assertEqual(body["order_type"], "market_order")
        self.assertEqual(body["time_in_force"], "ioc")
        self.assertEqual(body["side"], "buy")
        self.assertIsInstance(body["size"], int)

    def test_the_bracket_carries_stop_target_and_trail(self):
        b = self.c.build_bracket("ethusdt", True, 2511.25, 2543.49, 10.75)
        self.assertEqual(b["bracket_stop_loss_price"], "2511.25")
        self.assertEqual(b["bracket_take_profit_price"], "2543.49")
        self.assertEqual(b["bracket_trail_amount"], "10.75")
        self.assertEqual(b["bracket_stop_trigger_method"], "mark_price")

    def test_a_short_trail_is_negative(self):
        """The venue's own client signs it by side; forgetting inverts the trail."""
        b = self.c.build_bracket("ethusdt", False, 2532.75, 2500.5, 10.75)
        self.assertEqual(b["bracket_trail_amount"], "-10.75")

    def test_a_bracket_without_a_target_omits_it(self):
        self.assertNotIn("bracket_take_profit_price",
                         self.c.build_bracket("ethusdt", True, 2511.25))

    def test_dry_run_builds_both_bodies_and_sends_nothing(self):
        import asyncio
        out = asyncio.run(self.c.place("ethusdt", True, 2522.0, 1, 2511.25,
                                       2543.49, 10.75))
        self.assertTrue(out["dry_run"])
        self.assertIn("entry", out)
        self.assertIn("bracket", out)
        self.assertAlmostEqual(out["notional_inr"], 2572.44, places=2)


if __name__ == "__main__":
    unittest.main()


class TestPriceDivergence(unittest.TestCase):
    """
    Signals are priced on CoinDCX INR futures; orders go to Delta. Observed
    live: an ETH signal quoting 2517.00 against a Delta mid of 2501.83. On that
    SHORT the take profit sat at 2504.26 — above the price the order would
    actually fill at — so the trade was past its target before it opened and
    the only direction left was the stop.
    """

    def client(self):
        c = DeltaTradingClient("key", "secret", enabled=True, dry_run=True,
                               max_notional_inr=3000.0)
        c._specs["ethusdt"] = ContractSpec(1282, "ETHUSD", 0.01, 0.1)
        return c

    def test_the_observed_gap_is_refused(self):
        with self.assertRaises(OrderRefused) as ctx:
            self.client().build_entry("ethusdt", False, 2517.0, 1, 2529.74,
                                      venue_price=2501.83)
        self.assertIn("apart", str(ctx.exception))

    def test_ordinary_drift_is_allowed(self):
        self.assertTrue(self.client().build_entry(
            "ethusdt", False, 2517.0, 1, 2529.74, venue_price=2515.0))

    def test_no_venue_price_means_no_opinion(self):
        """Absent data is not evidence the prices agree — but it cannot veto."""
        self.assertTrue(self.client().build_entry(
            "ethusdt", False, 2517.0, 1, 2529.74))

    def test_the_threshold_is_smaller_than_a_typical_target(self):
        from collectors.delta_trading import MAX_PRICE_DIVERGENCE_PCT
        self.assertLess(MAX_PRICE_DIVERGENCE_PCT, 0.00504)


class TestRequestedMargin(unittest.TestCase):
    def margin(self, raw):
        from scheduler.health import _requested_margin
        return _requested_margin(raw)

    def test_a_typed_value_is_used(self):
        self.assertEqual(self.margin("250"), 250.0)

    def test_junk_falls_back_to_the_default(self):
        from config.settings import settings
        for raw in (None, "", "abc", "-5", "0"):
            with self.subTest(raw=raw):
                self.assertEqual(self.margin(raw), settings.delta_margin_inr)

    def test_it_cannot_exceed_what_the_cap_allows(self):
        """
        Refusing at the box rather than after a round trip to the venue: a
        margin that could never pass the notional cap is not a size, it is a
        typo.
        """
        from config.settings import settings
        ceiling = settings.delta_max_notional_inr / settings.delta_leverage
        self.assertEqual(self.margin("999999"), ceiling)
