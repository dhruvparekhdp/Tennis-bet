"""
Futures-only instruments, and showing a signal's age.

XAUUSDT read $0.000000 on the dashboard for hours while trading normally in
the app. CoinDCX's /exchange/ticker is spot-only, and gold exists there only
as a futures instrument — so the symbol matched nothing and silently rendered
a zero as though it were a price.
"""
import unittest
from datetime import UTC, datetime

from collectors.coindcx import (
    _base_symbol,
    _extract_price,
    _futures_name_variants,
)


class TestFuturesNaming(unittest.TestCase):
    def test_variants_cover_the_documented_prefixes(self):
        v = _futures_name_variants("xau")
        self.assertIn("B-XAU_USDT", v)
        self.assertIn("XAU_USDT", v)
        self.assertIn("XAUUSDT", v)

    def test_variants_are_upper_case_regardless_of_input(self):
        self.assertEqual(_futures_name_variants("xau"), _futures_name_variants("XAU"))

    def test_base_symbol_strips_the_quote(self):
        self.assertEqual(_base_symbol("xauusdt"), "xau")
        self.assertEqual(_base_symbol("btcusdt"), "btc")

    def test_a_bare_base_is_left_alone(self):
        self.assertEqual(_base_symbol("xau"), "xau")


class TestPriceExtraction(unittest.TestCase):
    """
    The futures payload shape is undocumented and was unreachable from the
    environment this was written in, so the parser accepts several shapes
    rather than betting on one and failing silently.
    """

    def test_reads_a_bare_number(self):
        self.assertEqual(_extract_price(4345.8), 4345.8)

    def test_reads_the_common_key_names(self):
        for key in ("ls", "last_price", "mark_price", "price", "c", "close"):
            with self.subTest(key=key):
                self.assertEqual(_extract_price({key: "4345.80"}), 4345.8)

    def test_returns_none_rather_than_zero_for_junk(self):
        """None means 'no data'; 0.0 would render as a price."""
        self.assertIsNone(_extract_price({"unrelated": 1}))
        self.assertIsNone(_extract_price(None))
        self.assertIsNone(_extract_price("nonsense"))
        self.assertIsNone(_extract_price({}))

    def test_zero_is_treated_as_missing_not_as_a_price(self):
        self.assertIsNone(_extract_price(0))
        self.assertIsNone(_extract_price({"last_price": 0}))

    def test_a_non_numeric_value_does_not_raise(self):
        self.assertIsNone(_extract_price({"last_price": "n/a"}))


class TestFuturesFallback(unittest.IsolatedAsyncioTestCase):
    async def _collector(self, payload):
        from analysis.crypto_state_store import CryptoStateStore
        from collectors.coindcx import CoinDCXCollector

        store = CryptoStateStore()
        col = CoinDCXCollector(store)

        async def fake_raw():
            return ("http://test", payload)

        col.fetch_futures_raw = fake_raw
        return col, store

    async def test_a_futures_only_symbol_gets_a_price(self):
        col, store = await self._collector({"B-XAU_USDT": {"ls": "4345.80"}})
        matched = await col._fetch_futures(["xauusdt"], datetime.now(UTC))
        self.assertEqual(matched, {"xauusdt"})
        state = await store.get("xauusdt")
        self.assertAlmostEqual(state.current_price, 4345.8, places=4)

    async def test_a_wrapped_payload_is_unwrapped(self):
        col, store = await self._collector({"prices": {"B-XAU_USDT": 4345.8}})
        self.assertEqual(await col._fetch_futures(["xauusdt"], datetime.now(UTC)),
                         {"xauusdt"})

    async def test_an_unknown_symbol_matches_nothing_rather_than_zero(self):
        col, store = await self._collector({"B-XAU_USDT": {"ls": "4345.80"}})
        self.assertEqual(await col._fetch_futures(["dogeusdt"], datetime.now(UTC)), set())

    async def test_an_unexpected_shape_is_survived(self):
        col, _ = await self._collector(["not", "a", "mapping"])
        self.assertEqual(await col._fetch_futures(["xauusdt"], datetime.now(UTC)), set())

    async def test_no_endpoint_answering_is_survived(self):
        from analysis.crypto_state_store import CryptoStateStore
        from collectors.coindcx import CoinDCXCollector

        col = CoinDCXCollector(CryptoStateStore())

        async def none_raw():
            return None

        col.fetch_futures_raw = none_raw
        self.assertEqual(await col._fetch_futures(["xauusdt"], datetime.now(UTC)), set())


class TestSignalTimestampSurface(unittest.TestCase):
    """The card now says when it fired, which also reveals a stale deploy."""

    def setUp(self):
        import scheduler.health as health
        self.html = health._HTML

    def test_the_formatter_exists_and_is_used(self):
        self.assertIn("function fmtSignalTime(", self.html)
        self.assertIn("fmtSignalTime(s.timestamp)", self.html)

    def test_it_shows_both_a_clock_time_and_an_age(self):
        self.assertIn("IST · ", self.html)
        self.assertIn("ago", self.html)

    def test_a_missing_timestamp_does_not_render_nan(self):
        self.assertIn("time unknown", self.html)

    def test_a_symbol_with_no_feed_is_not_shown_as_a_price(self):
        self.assertIn("no price feed", self.html)
        self.assertIn("/api/debug/coindcx", self.html)

    def test_the_probe_endpoint_is_registered(self):
        import inspect

        import scheduler.health as health
        self.assertIn('"/api/debug/coindcx"', inspect.getsource(health))
