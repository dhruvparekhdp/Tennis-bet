"""
Why one symbol dominated the feed, and making the filter follow the watchlist.

Four BCHUSDT longs fired inside an hour — 1:31, 1:57, 2:17, 2:32 — each the
same continuous move at a later price. The 15-minute cooldown is per
(symbol, setup), so a coin in a strong trend re-qualified every time it
expired and crowded every other symbol off the page.
"""
import unittest
from datetime import UTC, datetime, timedelta

from analysis.crypto_engine import CryptoEngine
from analysis.crypto_signal import CryptoSignal


def sig(symbol="bchusdt", direction="long", price=262.34, target=263.67, stop=261.01):
    return CryptoSignal(
        symbol=symbol, signal_type="confluence", direction=direction,
        trigger_description="x", confidence=0.71, current_price=price,
        target_price=target, stop_loss=stop, edge_pct=0.5, stake_pct=0.005,
        timeframe="1h", sentiment_score=0.0, indicators_summary="",
        timestamp=datetime.now(UTC),
    )


class TestLiveSignalSuppression(unittest.TestCase):
    def setUp(self):
        self.eng = CryptoEngine()

    def test_a_repeat_is_suppressed_while_the_first_is_unresolved(self):
        first = sig()
        self.eng._live[(first.symbol, first.direction)] = first
        # Price still between stop and target: the earlier call is neither
        # right nor wrong yet.
        self.assertTrue(self.eng._still_live(sig(price=262.9), 262.9))

    def test_it_reopens_once_the_target_is_hit(self):
        first = sig()
        self.eng._live[(first.symbol, first.direction)] = first
        self.assertFalse(self.eng._still_live(sig(price=263.80), 263.80))

    def test_it_reopens_once_the_stop_is_hit(self):
        first = sig()
        self.eng._live[(first.symbol, first.direction)] = first
        self.assertFalse(self.eng._still_live(sig(price=260.90), 260.90))

    def test_a_resolved_signal_is_forgotten_rather_than_left_to_grow(self):
        first = sig()
        self.eng._live[(first.symbol, first.direction)] = first
        self.eng._still_live(sig(price=263.80), 263.80)
        self.assertNotIn((first.symbol, first.direction), self.eng._live)

    def test_the_opposite_direction_is_not_blocked(self):
        """A reversal is genuinely new information."""
        first = sig(direction="long")
        self.eng._live[(first.symbol, first.direction)] = first
        self.assertFalse(self.eng._still_live(sig(direction="short"), 262.9))

    def test_another_symbol_is_not_blocked(self):
        """The whole point: one busy coin must not silence the others."""
        first = sig(symbol="bchusdt")
        self.eng._live[(first.symbol, first.direction)] = first
        self.assertFalse(self.eng._still_live(sig(symbol="ethusdt"), 262.9))

    def test_shorts_resolve_the_other_way_round(self):
        first = sig(direction="short", price=262.34, target=261.01, stop=263.67)
        self.eng._live[(first.symbol, first.direction)] = first
        self.assertTrue(self.eng._still_live(sig(direction="short"), 262.0))
        self.assertFalse(self.eng._still_live(sig(direction="short"), 260.5))

    def test_no_previous_signal_means_no_suppression(self):
        self.assertFalse(self.eng._still_live(sig(), 262.9))

    def test_a_missing_price_does_not_suppress(self):
        """Absent data must not silently mute the feed."""
        first = sig()
        self.eng._live[(first.symbol, first.direction)] = first
        self.assertFalse(self.eng._still_live(sig(), 0.0))


class TestCooldownStillApplies(unittest.TestCase):
    def test_the_timer_is_per_symbol_and_setup(self):
        eng = CryptoEngine()
        eng._set_cooldown("bchusdt", "confluence", datetime.now(UTC))
        self.assertTrue(eng._is_on_cooldown("bchusdt", "confluence"))
        self.assertFalse(eng._is_on_cooldown("bchusdt", "bollinger_squeeze"))
        self.assertFalse(eng._is_on_cooldown("ethusdt", "confluence"))

    def test_it_expires(self):
        eng = CryptoEngine()
        eng._set_cooldown("bchusdt", "confluence",
                          datetime.now(UTC) - timedelta(hours=2))
        self.assertFalse(eng._is_on_cooldown("bchusdt", "confluence"))


class TestTabsFollowTheWatchlist(unittest.TestCase):
    def setUp(self):
        import scheduler.health as health
        self.html = health._HTML

    def test_tabs_are_built_from_the_watchlist_not_just_from_signals(self):
        """
        A quiet coin used to vanish from the filter — the one case where you
        most want to check whether anything fired.
        """
        self.assertIn("_crWatchlistSymbols", self.html)
        self.assertIn("[..._crWatchlistSymbols, ...withSignals]", self.html)

    def test_the_watchlist_render_feeds_the_tabs(self):
        self.assertIn("_crWatchlistSymbols = (coins||[])", self.html)

    def test_each_tab_carries_its_count(self):
        self.assertIn("cr-tab-n", self.html)

    def test_a_symbol_with_no_signals_is_dimmed_rather_than_hidden(self):
        self.assertIn(".cr-sig-tab.quiet", self.html)
