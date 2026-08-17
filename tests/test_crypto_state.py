import unittest
import asyncio
from datetime import datetime, timezone

from analysis.crypto_state import CryptoState, OHLCVCandle, CommodityState, NewsItem
from analysis.crypto_state_store import (
    CryptoStateStore,
    CommodityStateStore,
    _compute_rsi,
    _compute_bollinger,
    _compute_ema,
    _compute_atr,
)


class TestCryptoState(unittest.TestCase):
    def test_rsi_calculation(self):
        prices = [100.0 + i * 2.0 for i in range(20)]
        rsi = _compute_rsi(prices, 14)
        self.assertGreater(rsi, 80.0)

        falling = [200.0 - i * 3.0 for i in range(20)]
        rsi_falling = _compute_rsi(falling, 14)
        self.assertLess(rsi_falling, 20.0)

    def test_bollinger_calculation(self):
        closes = [100.0 + (i % 3) for i in range(30)]
        upper, mid, lower, bw = _compute_bollinger(closes, 20, 2.0)
        self.assertGreater(upper, mid)
        self.assertGreater(mid, lower)
        self.assertGreater(bw, 0)

    def test_crypto_state_store_kline(self):
        async def _run():
            store = CryptoStateStore()
            now = datetime.now(timezone.utc)

            for i in range(25):
                price = 50000.0 + i * 100.0
                await store.update_kline(
                    symbol="btcusdt",
                    open_=price - 10,
                    high=price + 50,
                    low=price - 20,
                    close=price,
                    volume=15.0 + i,
                    timestamp=now,
                    is_closed=True,
                )

            state = await store.get("btcusdt")
            self.assertIsNotNone(state)
            self.assertEqual(state.current_price, 50000.0 + 24 * 100.0)
            self.assertEqual(len(state.candles_1m), 25)
            self.assertGreater(state.rsi_14, 50.0)
            self.assertGreater(state.bollinger_mid, 0)

        asyncio.run(_run())

    def test_crypto_state_store_watchlist_management(self):
        async def _run():
            store = CryptoStateStore()
            self.assertGreaterEqual(await store.count(), 50)

            await store.add_symbol("kasusdt")
            state = await store.get("kasusdt")
            self.assertIsNotNone(state)
            self.assertEqual(state.base_asset, "KAS")

            await store.remove_symbol("kasusdt")
            self.assertIsNone(await store.get("kasusdt"))

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
