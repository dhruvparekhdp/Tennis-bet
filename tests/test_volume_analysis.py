"""
Volume checks, and the feed defect that made them necessary.

The volume family was voting on a constant for as long as the REST poller has
existed: it stamped the rolling 24-hour total onto every one-minute bar, so
relative volume was always 1.0, VWAP collapsed to an unweighted average and
money flow became a price oscillator wearing a volume label. These tests pin
both halves — that a fabricated series is refused, and that a real one is read.
"""
import math
import unittest
from datetime import UTC, datetime, timedelta

from analysis import indicators as ind
from analysis.confluence import (
    MIN_RELATIVE_VOLUME,
    Family,
    thin_volume_veto,
    volume_vote,
)
from analysis.crypto_state import OHLCVCandle

T0 = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)


def series(n=40, base=100.0, step=0.0):
    return [base + i * step for i in range(n)]


class TestVolumeProvenance(unittest.TestCase):
    def test_a_real_series_is_usable(self):
        vols = [80.0 + (i % 7) * 9 for i in range(40)]
        self.assertTrue(ind.has_usable_volume(vols))

    def test_a_rolling_24h_total_stamped_on_every_bar_is_refused(self):
        """The exact shape the REST poller used to write. Data-looking, not data."""
        self.assertFalse(ind.has_usable_volume([5_000_000.0] * 40))

    def test_an_all_zero_series_is_refused(self):
        self.assertFalse(ind.has_usable_volume([0.0] * 40))

    def test_a_short_series_is_refused_rather_than_guessed_at(self):
        self.assertFalse(ind.has_usable_volume([80.0, 120.0, 95.0]))

    def test_negative_volume_anywhere_refuses_the_whole_series(self):
        """A negative reading means a broken feed, not one odd bar to skip."""
        for at in (0, 10, 39):
            vols = [80.0 + (i % 7) * 9 for i in range(40)]
            vols[at] = -5.0
            with self.subTest(at=at):
                self.assertFalse(ind.has_usable_volume(vols))


class TestRelativeVolume(unittest.TestCase):
    def test_an_ordinary_bar_reads_near_one(self):
        vols = [100.0 + (i % 5) for i in range(40)]
        self.assertAlmostEqual(ind.relative_volume(vols), 1.0, delta=0.1)

    def test_a_spike_is_detected(self):
        vols = [100.0 + (i % 5) for i in range(40)]
        vols[-1] = 400.0
        self.assertGreater(ind.relative_volume(vols), 3.0)

    def test_the_current_bar_is_excluded_from_its_own_baseline(self):
        """Including it damps exactly the spike the measure exists to find."""
        vols = [100.0 + (i % 5) for i in range(40)]
        vols[-1] = 2000.0
        # 2000 / ~102, not 2000 / (a mean dragged upward by the 2000 itself).
        self.assertGreater(ind.relative_volume(vols), 15.0)

    def test_a_dead_tape_reads_below_the_floor(self):
        vols = [100.0 + (i % 5) for i in range(40)]
        vols[-1] = 20.0
        self.assertLess(ind.relative_volume(vols), MIN_RELATIVE_VOLUME)

    def test_an_unusable_feed_returns_none_not_a_number(self):
        self.assertIsNone(ind.relative_volume([5_000_000.0] * 40))


class TestVolumeTrend(unittest.TestCase):
    def test_volume_arriving_on_up_bars_reads_positive(self):
        closes = series(40, step=0.1)
        vols = [100.0 + (i % 5) for i in range(40)]
        self.assertGreater(ind.volume_trend(closes, vols), 0.9)

    def test_volume_arriving_on_down_bars_reads_negative(self):
        closes = list(reversed(series(40, step=0.1)))
        vols = [100.0 + (i % 5) for i in range(40)]
        self.assertLess(ind.volume_trend(closes, vols), -0.9)

    def test_it_is_bounded_so_symbols_can_be_compared(self):
        """An OBV of 4.2 million means nothing without knowing the coin."""
        for step in (0.0, 0.1, -0.1):
            closes = series(40, step=step)
            vols = [1e9 + (i % 5) * 1e7 for i in range(40)]
            got = ind.volume_trend(closes, vols)
            if got is not None:
                with self.subTest(step=step):
                    self.assertGreaterEqual(got, -1.0)
                    self.assertLessEqual(got, 1.0)


class TestThinVolumeVeto(unittest.TestCase):
    def test_a_dried_up_tape_is_vetoed(self):
        vols = [100.0 + (i % 5) for i in range(40)]
        vols[-1] = 20.0
        self.assertIsNotNone(thin_volume_veto(vols))

    def test_a_normal_tape_is_not_vetoed(self):
        vols = [100.0 + (i % 5) for i in range(40)]
        self.assertIsNone(thin_volume_veto(vols))

    def test_a_missing_measurement_never_vetoes(self):
        """
        Absent data is not evidence of a thin market. Vetoing here would have
        silenced every REST-fed symbol at once.
        """
        self.assertIsNone(thin_volume_veto([5_000_000.0] * 40))
        self.assertIsNone(thin_volume_veto([0.0] * 40))
        self.assertIsNone(thin_volume_veto([]))


class TestVolumeVote(unittest.TestCase):
    def bars(self, n=60):
        """
        A drifting series with pullbacks, not a straight line. A monotonic
        rise pins money flow at 100 and the family's own components cancel —
        correctly, but it makes for a fixture that tests nothing.
        """
        closes, p = [], 100.0
        for i in range(n):
            p += math.sin(i / 4.0) * 0.35 + 0.02
            closes.append(round(p, 4))
        return ([c + 0.06 for c in closes], [c - 0.06 for c in closes], closes)

    def test_it_abstains_when_the_feed_has_no_per_bar_volume(self):
        """
        The whole point. A family that always votes is indistinguishable from
        a family that knows something.
        """
        highs, lows, closes = self.bars()
        vote = volume_vote(highs, lows, closes, [5_000_000.0] * 60)
        self.assertEqual(vote.family, Family.VOLUME)
        self.assertEqual(vote.direction, 0)
        self.assertEqual(vote.weight, 0.0)

    def test_it_votes_when_the_feed_is_real(self):
        highs, lows, closes = self.bars()
        vols = [100.0 + (i % 5) for i in range(60)]
        self.assertNotEqual(volume_vote(highs, lows, closes, vols).direction, 0)

    def test_a_thin_tape_weighs_less_than_a_busy_one(self):
        """Participation does not pick a side, so it scales weight, not score."""
        highs, lows, closes = self.bars()
        busy = [100.0 + (i % 5) for i in range(60)]
        thin = list(busy)
        thin[-1] = 30.0
        a = volume_vote(highs, lows, closes, busy)
        b = volume_vote(highs, lows, closes, thin)
        self.assertEqual(a.direction, b.direction)
        self.assertLess(b.weight, a.weight)

    def test_the_weight_never_leaves_its_range(self):
        highs, lows, closes = self.bars()
        for spike in (5.0, 100.0, 400.0, 5000.0):
            vols = [100.0 + (i % 5) for i in range(60)]
            vols[-1] = spike
            with self.subTest(spike=spike):
                w = volume_vote(highs, lows, closes, vols).weight
                self.assertGreaterEqual(w, 0.0)
                self.assertLessEqual(w, 1.0)


class TestRestPathWritesHonestVolume(unittest.TestCase):
    async def _poll(self):
        from analysis.crypto_state_store import CryptoStateStore

        store = CryptoStateStore()
        start = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
        for i in range(6):
            await store.update_from_rest(
                symbol="ethusdt", price=1900.0 + i, high_24h=1950.0, low_24h=1850.0,
                volume_24h=5_000_000.0, change_24h_pct=1.0,
                timestamp=start + timedelta(minutes=i))
        return (await store.get("ethusdt")).candles_1m

    def test_the_24h_total_is_not_stamped_onto_a_one_minute_bar(self):
        import asyncio

        candles = asyncio.run(self._poll())
        self.assertTrue(candles)
        self.assertTrue(all(c.volume == 0.0 for c in candles),
                        [c.volume for c in candles])

    def test_the_bars_still_carry_real_price_movement(self):
        """Zeroing volume must not disturb the range fix that revived ATR."""
        import asyncio

        candles = asyncio.run(self._poll())
        self.assertEqual(len(candles), 6)
        self.assertEqual([c.close for c in candles],
                         [1900.0 + i for i in range(6)])


class TestHorizons(unittest.TestCase):
    """
    The fixed horizon list is gone. It asked whether a market fit a window we
    had already chosen; since the cost floor beat the volatility term every
    time, the answer was the same 0.505% target on every coin at every hour.
    The hold is now derived from the target and the market's own range.
    """

    def test_the_time_to_a_move_scales_with_the_square_of_the_distance(self):
        from analysis.scalp_levels import ScalpConfig
        cfg = ScalpConfig()
        near = cfg.minutes_to_move(0.005, 0.001)
        far = cfg.minutes_to_move(0.010, 0.001)
        self.assertAlmostEqual(far / near, 4.0, places=6)

    def test_a_quieter_market_needs_longer_for_the_same_move(self):
        from analysis.scalp_levels import ScalpConfig
        cfg = ScalpConfig()
        self.assertGreater(cfg.minutes_to_move(0.01, 0.0006),
                           cfg.minutes_to_move(0.01, 0.0018))

    def test_a_market_that_cannot_get_there_in_time_is_refused(self):
        """
        BTC's real 0.059% bar range puts its target 268 minutes out, which the
        six-hour hold accepts. A market a third as lively does not get there
        inside any hold worth calling short.
        """
        from analysis.scalp_levels import NoTrade, ScalpConfig, scalp_levels
        got = scalp_levels(78566.0, True, 0.0002,
                           ScalpConfig().for_symbol("btcusdt"), symbol="btcusdt")
        self.assertIs(got, NoTrade.TOO_SLOW)

    def test_the_largest_markets_are_slow_but_not_refused(self):
        """
        At a four-hour cap BTC and BNB were permanently refused. Their real bar
        ranges put a cost-clearing target four to five hours out — a long
        forecast, not a bad one.
        """
        from analysis.scalp_levels import ScalpConfig, ScalpLevels, scalp_levels
        cfg = ScalpConfig()
        for sym, px, atr in (("btcusdt", 78586.0, 0.000587),
                             ("bnbusdt", 688.29, 0.000559)):
            got = scalp_levels(px, True, atr, cfg.for_symbol(sym), symbol=sym)
            with self.subTest(symbol=sym):
                self.assertIsInstance(got, ScalpLevels)
                self.assertGreater(got.horizon_minutes, 240)
                self.assertLessEqual(got.horizon_minutes, cfg.max_hold_minutes)

    def test_a_lively_market_gets_a_short_hold_and_a_quiet_one_a_long_hold(self):
        from analysis.scalp_levels import ScalpConfig, ScalpLevels, scalp_levels
        cfg = ScalpConfig()
        fast = scalp_levels(103.32, True, 0.0018, cfg.for_symbol("solusdt"),
                            symbol="solusdt")
        slow = scalp_levels(2451.0, True, 0.0010, cfg.for_symbol("ethusdt"),
                            symbol="ethusdt")
        self.assertIsInstance(fast, ScalpLevels)
        self.assertIsInstance(slow, ScalpLevels)
        self.assertLess(fast.horizon_minutes, slow.horizon_minutes)


class TestTheFormingBarIsNotCountedAsVolume(unittest.TestCase):
    """
    The last kline is the minute in progress. Its high, low and close are real
    — that is what has happened so far — but its volume is not comparable to a
    completed bar's, and comparing them is what took the live board out: five
    symbols refused for "volume is 0.00x its recent average" while the venue
    was trading perfectly normally.
    """

    def candles(self, n=60, last_volume=0.4):
        out = []
        for i in range(n):
            price = 100.0 + math.sin(i / 5.0) * 0.4
            out.append(OHLCVCandle(price, price + 0.15, price - 0.15, price,
                                   100.0 + (i % 7) * 9,
                                   T0 + timedelta(minutes=i), is_closed=True))
        out.append(OHLCVCandle(100.0, 100.1, 99.9, 100.0, last_volume,
                               T0 + timedelta(minutes=n), is_closed=False))
        return out

    def test_the_forming_bar_alone_reads_as_a_dead_tape(self):
        """The bug, stated as a fact about the data."""
        all_bars = [c.volume for c in self.candles()]
        self.assertLess(ind.relative_volume(all_bars), 0.05)

    def test_excluding_it_reads_as_a_normal_one(self):
        closed = [c.volume for c in self.candles() if c.is_closed]
        self.assertAlmostEqual(ind.relative_volume(closed), 1.0, delta=0.25)

    def test_the_vote_no_longer_vetoes_a_normally_trading_market(self):
        from analysis.confluence import evaluate
        v = evaluate(self.candles(200), min_atr_pct=0.0001)
        self.assertFalse(any("0.00x" in reason for reason in v.vetoes), v.vetoes)

    def test_a_genuinely_thin_tape_still_vetoes(self):
        """The guard must still work on real drying-up, not just be switched off."""
        bars = self.candles(200)
        for c in bars[-6:]:
            c.volume = 4.0
        volumes = [c.volume for c in bars if c.is_closed]
        self.assertLess(ind.relative_volume(volumes), MIN_RELATIVE_VOLUME)
