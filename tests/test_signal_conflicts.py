"""
Two open, contradictory calls on one market.

Observed on the live feed: a confluence SHORT on ETH at 11:17 backed by three
independent families, then a volume-spike LONG on the same coin at 11:24, off
the same volume event. Acting on both means paying two round trips to hold
nothing at all.

Neither existing guard could see it, and the reason is worth stating: `_live`
is keyed by (symbol, direction), so a running short never looks at longs; the
cooldown is keyed by (symbol, signal_type), so one analyzer firing never
quiets another. Both are duplicate suppressors. Neither is a contradiction
check.
"""
import unittest
from datetime import UTC, datetime, timedelta

from analysis.crypto_engine import CryptoEngine
from analysis.crypto_signal import CryptoSignal


def signal(direction="short", kind="confluence", price=2428.01,
           minutes_ago=0, symbol="ethusdt", confidence=0.74):
    long_ = direction == "long"
    move = 0.0068
    return CryptoSignal(
        symbol=symbol, signal_type=kind, direction=direction,
        trigger_description="x", confidence=confidence, current_price=price,
        target_price=price * (1 + move) if long_ else price * (1 - move),
        stop_loss=price * (1 - move / 2) if long_ else price * (1 + move / 2),
        edge_pct=0.5, stake_pct=0.005, timeframe="93m", sentiment_score=0.0,
        indicators_summary="",
        timestamp=datetime.now(UTC) - timedelta(minutes=minutes_ago))


class TestOpposingSignalsCannotBothBeOpen(unittest.TestCase):
    def setUp(self):
        self.engine = CryptoEngine()

    def live_short(self, price=2428.01):
        sig = signal("short", "confluence", price)
        self.engine._live[(sig.symbol, "short")] = sig
        return sig

    def test_the_screenshot_case_is_refused(self):
        """A volume-spike long, seven minutes after a confluence short."""
        self.live_short()
        got = self.engine._opposing_live(
            signal("long", "volume_spike", 2424.22, minutes_ago=0), 2424.22)
        self.assertIsNotNone(got)
        self.assertEqual(got.direction, "short")

    def test_a_same_direction_signal_is_not_a_contradiction(self):
        self.live_short()
        self.assertIsNone(self.engine._opposing_live(
            signal("short", "volume_spike", 2424.22), 2424.22))

    def test_nothing_live_means_nothing_to_contradict(self):
        self.assertIsNone(self.engine._opposing_live(signal("long"), 2424.22))

    def test_the_field_clears_once_the_live_call_is_stopped_out(self):
        """
        The stop being hit IS the position saying it was wrong. Until then a
        second opinion the other way is churn, not information — but after it,
        the reversal is exactly what should be allowed.
        """
        prev = self.live_short()
        self.assertIsNone(self.engine._opposing_live(
            signal("long"), prev.stop_loss + 1))

    def test_the_field_clears_once_the_live_call_hits_its_target(self):
        prev = self.live_short()
        self.assertIsNone(self.engine._opposing_live(
            signal("long"), prev.target_price - 1))

    def test_an_expired_call_no_longer_blocks(self):
        sig = signal("short", "confluence", minutes_ago=600)
        self.engine._live[(sig.symbol, "short")] = sig
        self.assertIsNone(self.engine._opposing_live(signal("long"), 2424.22))

    def test_a_different_symbol_is_unaffected(self):
        self.live_short()
        self.assertIsNone(self.engine._opposing_live(
            signal("long", symbol="solusdt"), 104.0))

    def test_the_guard_is_symmetric(self):
        sig = signal("long", "volume_spike")
        self.engine._live[(sig.symbol, "long")] = sig
        self.assertIsNotNone(self.engine._opposing_live(
            signal("short", "confluence"), sig.current_price))


class TestVolumeSurgeCannotOutvoteStructure(unittest.TestCase):
    """
    The analyzer's own comment claimed a surge "confirms whichever way the bar
    closed, it does not pick the direction itself", while the code elected a
    direction from one candle body against anything at all.
    """

    def state(self, rising=True, last_bar_up=True):
        import random

        from analysis.crypto_state import CryptoState, OHLCVCandle
        rng = random.Random(2)
        st = CryptoState(symbol="ethusdt", base_asset="ETH", current_price=2428.0)
        p, step = 2400.0, (0.0009 if rising else -0.0009)
        t = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
        for i in range(120):
            o = p
            c = o * (1 + step + rng.gauss(0, 0.0002))
            st.candles_1m.append(OHLCVCandle(
                o, max(o, c) * 1.0004, min(o, c) * 0.9996, c,
                100.0 + (i % 5) * 8, t + timedelta(minutes=i), is_closed=True))
            p = c
        # The surge bar, pointing whichever way the test asks.
        o = p
        c = o * (1.004 if last_bar_up else 0.996)
        st.candles_1m.append(OHLCVCandle(
            o, max(o, c) * 1.001, min(o, c) * 0.999, c, 900.0,
            t + timedelta(minutes=120), is_closed=False))
        st.current_price = c
        st.atr_14 = c * 0.0012
        return st

    def analyzer(self):
        from analysis.crypto_signals import VolumeSpikeAnalyzer
        return VolumeSpikeAnalyzer()

    def test_a_surge_against_the_structure_stands_aside(self):
        """The live failure: an up bar inside a sequence of lower lows."""
        self.assertIsNone(self.analyzer().analyze(
            self.state(rising=False, last_bar_up=True)))

    def test_a_surge_with_the_structure_is_allowed_through_the_check(self):
        from analysis.indicators import trend_structure
        st = self.state(rising=True, last_bar_up=True)
        structure = trend_structure([c.high for c in st.candles_1m],
                                    [c.low for c in st.candles_1m])
        self.assertNotEqual(structure, -1)


class TestVolumeShareIsReportedAsAShare(unittest.TestCase):
    """
    volume_trend returns an imbalance in -1..+1, not a share. At -0.44 sellers
    do not own 44% of the volume — they own 72% — so the alert was
    understating the very evidence it was citing.
    """

    def test_the_reported_share_is_the_share(self):
        from analysis.confluence import volume_vote
        closes, highs, lows, volumes = [], [], [], []
        p = 100.0
        for i in range(60):
            down = i % 4 != 0                     # mostly selling
            p *= (0.999 if down else 1.0015)
            closes.append(p)
            highs.append(p * 1.0005)
            lows.append(p * 0.9995)
            volumes.append(140.0 if down else 40.0)
        vote = volume_vote(highs, lows, closes, volumes)
        pct = [int(tok.rstrip("%")) for tok in vote.reason.replace("%", "% ").split()
               if tok.endswith("%") and tok[:-1].isdigit()]
        self.assertTrue(pct, vote.reason)
        # A share, so it must be a majority — never a number below half.
        for value in pct:
            self.assertGreaterEqual(value, 50, vote.reason)
            self.assertLessEqual(value, 100, vote.reason)


if __name__ == "__main__":
    unittest.main()
