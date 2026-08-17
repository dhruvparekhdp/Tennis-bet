import unittest
from analysis.crypto_state import CryptoState
from analysis.multi_horizon_predictor import MultiHorizonPredictor


class TestCryptoMultiHorizon(unittest.TestCase):
    def test_multi_horizon_predictions(self):
        predictor = MultiHorizonPredictor()
        state = CryptoState(symbol="btcusdt", base_asset="BTC", current_price=68000.0)
        state.rsi_14 = 28.0  # Oversold condition
        state.ema_20 = 67500.0
        state.ema_50 = 66000.0
        state.sentiment_score = 0.40

        forecasts = predictor.predict_all_horizons(state)
        self.assertIn("30m", forecasts)
        self.assertIn("1h", forecasts)
        self.assertIn("4h", forecasts)
        self.assertIn("1d", forecasts)

        for h, f in forecasts.items():
            self.assertEqual(f.horizon, h)
            self.assertIn(f.direction, ("up", "down", "neutral"))
            self.assertTrue(0.0 <= f.confidence <= 1.0)
            self.assertGreater(f.target_price, 0)


if __name__ == "__main__":
    unittest.main()
