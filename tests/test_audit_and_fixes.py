import unittest
from datetime import UTC, datetime, timedelta

from analysis.crypto_signal import CryptoSignal
from analysis.crypto_state import CryptoState, OHLCVCandle, resample_candles
from collectors.coindcx import (
    CoinDCXCollector,
    _base_symbol,
    _futures_name_variants,
    _quote_symbol,
)


class TestQuoteIsolation(unittest.TestCase):
    def test_quote_symbol_extraction(self):
        self.assertEqual(_quote_symbol("xauusdt"), "USDT")
        self.assertEqual(_quote_symbol("btcusdt"), "USDT")
        self.assertEqual(_quote_symbol("xauinr"), "INR")
        self.assertEqual(_quote_symbol("btcinr"), "INR")

    def test_pair_variants_respects_quote_currency(self):
        from collectors.coindcx import CoinDCXCollector
        from analysis.crypto_state_store import CryptoStateStore
        col = CoinDCXCollector(CryptoStateStore())

        usdt_variants = col._pair_variants("xauusdt")
        self.assertIn("B-XAU_USDT", usdt_variants)
        self.assertNotIn("I-XAU_INR", usdt_variants)

        inr_variants = col._pair_variants("xauinr")
        self.assertIn("I-XAU_INR", inr_variants)
        self.assertNotIn("B-XAU_USDT", inr_variants)

    def test_futures_variants_respects_quote(self):
        usdt_v = _futures_name_variants("XAU", "USDT")
        self.assertIn("B-XAU_USDT", usdt_v)
        self.assertNotIn("B-XAU_INR", usdt_v)

        inr_v = _futures_name_variants("XAU", "INR")
        self.assertIn("B-XAU_INR", inr_v)
        self.assertNotIn("B-XAU_USDT", inr_v)


class TestCryptoEngineTTL(unittest.TestCase):
    def test_still_live_expires_after_ttl(self):
        from analysis.crypto_engine import CryptoEngine
        engine = CryptoEngine()

        now = datetime.now(UTC)
        past = now - timedelta(minutes=75)

        sig = CryptoSignal(
            symbol="ethusdt",
            signal_type="confluence",
            direction="long",
            trigger_description="test",
            confidence=0.75,
            current_price=2000.0,
            target_price=2040.0,
            stop_loss=1980.0,
            edge_pct=1.0,
            stake_pct=0.01,
            timeframe="30m",
            sentiment_score=0.0,
            indicators_summary="",
            timestamp=past,
        )

        engine._live[(sig.symbol, sig.direction)] = sig

        # Price between stop and target
        current_price = 2010.0
        # Should be False because 75 minutes exceeds the 45m TTL for a 30m signal
        self.assertFalse(engine._still_live(sig, current_price, now=now))
        self.assertNotIn((sig.symbol, sig.direction), engine._live)


class TestCandleResampling(unittest.TestCase):
    def test_resample_1m_to_15m(self):
        base_time = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)
        candles = []
        for i in range(30):
            t = base_time + timedelta(minutes=i)
            candles.append(OHLCVCandle(
                open=100.0 + i,
                high=105.0 + i,
                low=95.0 + i,
                close=101.0 + i,
                volume=10.0,
                timestamp=t,
                is_closed=True,
            ))

        resampled = resample_candles(candles, 15)
        self.assertEqual(len(resampled), 2)
        bar1, bar2 = resampled[0], resampled[1]

        self.assertEqual(bar1.open, 100.0)
        self.assertEqual(bar1.high, 105.0 + 14)
        self.assertEqual(bar1.low, 95.0)
        self.assertEqual(bar1.close, 101.0 + 14)
        self.assertEqual(bar1.volume, 150.0)

        self.assertEqual(bar2.open, 100.0 + 15)
        self.assertEqual(bar2.high, 105.0 + 29)
        self.assertEqual(bar2.low, 95.0 + 15)
        self.assertEqual(bar2.close, 101.0 + 29)
        self.assertEqual(bar2.volume, 150.0)


if __name__ == "__main__":
    unittest.main()
