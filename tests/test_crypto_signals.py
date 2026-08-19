import unittest
from datetime import datetime, timezone

from analysis.crypto_state import CryptoState, OHLCVCandle
from analysis.crypto_state_store import recalculate_indicators
from analysis.crypto_signals import (
    RSIDivergenceAnalyzer,
    VolumeSpikeAnalyzer,
    BollingerSqueezeAnalyzer,
    SentimentShiftAnalyzer,
)
from analysis.crypto_engine import CryptoEngine
from notifications.crypto_formatter import format_crypto_signal


class TestCryptoSignals(unittest.TestCase):
    def test_volume_spike_analyzer(self):
        analyzer = VolumeSpikeAnalyzer()
        state = CryptoState(symbol="ethusdt", base_asset="ETH", current_price=3000.0)
        state.volume_24h = 350000.0
        state.volume_24h_avg = 100000.0

        now = datetime.now(timezone.utc)
        for _ in range(20):
            state.candles_1m.append(OHLCVCandle(2990.0, 3010.0, 2985.0, 3000.0, 50.0, now))
        # Levels now come from real volatility, so the fixture has to produce a
        # real ATR rather than relying on a fabricated fallback.
        recalculate_indicators(state)

        sig = analyzer.analyze(state)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.signal_type, "volume_spike")
        self.assertIn(sig.direction, ("long", "short"))
        self.assertGreaterEqual(sig.confidence, 0.60)

        msg = format_crypto_signal(sig)
        self.assertIn("ETHUSDT", msg)
        # Message was simplified to plain English: a friendly signal name replaces
        # the old "CRYPTO TRADE SIGNAL" banner and the raw indicator dump.
        self.assertIn("Volume Surge", msg)
        self.assertIn("Entry", msg)
        self.assertIn("Confidence", msg)

    def test_sentiment_shift_analyzer(self):
        analyzer = SentimentShiftAnalyzer()
        state = CryptoState(symbol="solusdt", base_asset="SOL", current_price=150.0)
        state.sentiment_score = 0.65
        state.sentiment_news_count = 12
        state.rsi_14 = 48.0
        now = datetime.now(timezone.utc)
        for _ in range(20):
            state.candles_1m.append(OHLCVCandle(149.0, 151.0, 148.5, 150.0, 40.0, now))
        recalculate_indicators(state)
        state.rsi_14 = 48.0   # recompute overwrites it; the analyzer gates on this

        sig = analyzer.analyze(state)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.signal_type, "sentiment_shift")
        self.assertEqual(sig.direction, "long")

    def test_crypto_engine_cooldown(self):
        engine = CryptoEngine()
        state = CryptoState(symbol="btcusdt", base_asset="BTC", current_price=65000.0)
        state.volume_24h = 400000.0
        state.volume_24h_avg = 100000.0

        now = datetime.now(timezone.utc)
        for _ in range(20):
            state.candles_1m.append(OHLCVCandle(64900.0, 65100.0, 64850.0, 65000.0, 100.0, now))
        recalculate_indicators(state)

        signals_1 = engine.process(state)
        self.assertGreater(len(signals_1), 0)

        # Immediate second call should be caught by cooldown
        signals_2 = engine.process(state)
        self.assertEqual(len(signals_2), 0)


if __name__ == "__main__":
    unittest.main()
