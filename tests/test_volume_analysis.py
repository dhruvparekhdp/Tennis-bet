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
from analysis.scalp_levels import HORIZONS_MINUTES


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
    def test_the_short_window_is_fifteen_minutes(self):
        """
        Ten bars ask a market for 3.2x its per-bar range, thirty ask 5.5x — so
        the short window was admitting quieter markets and then giving them
        less time. Fifteen is 3.9x.
        """
        self.assertEqual(HORIZONS_MINUTES, (15, 30))

    def test_horizons_are_offered_shortest_first(self):
        self.assertEqual(list(HORIZONS_MINUTES), sorted(HORIZONS_MINUTES))

    def test_the_short_window_demands_more_volatility_than_the_long_one(self):
        from analysis.scalp_levels import ScalpConfig

        cfg = ScalpConfig()
        short = cfg.reachable_move_pct(0.001, HORIZONS_MINUTES[0])
        long_ = cfg.reachable_move_pct(0.001, HORIZONS_MINUTES[-1])
        self.assertLess(short, long_)


if __name__ == "__main__":
    unittest.main()


class TestCandleIngestion(unittest.TestCase):
    """
    Real bars are the actual fix: without them there is no per-bar volume to
    analyse, and the poll-aggregated history understates the true range too.
    """

    def bars(self, n=60):
        t = datetime(2026, 8, 20, tzinfo=UTC)
        return [{"timestamp": t + timedelta(minutes=i), "open": 1900.0 + i,
                 "high": 1901.0 + i, "low": 1899.0 + i, "close": 1900.5 + i,
                 "volume": 100.0 + (i % 7) * 9} for i in range(n)]

    def _install(self, bars):
        import asyncio

        from analysis.crypto_state_store import CryptoStateStore

        async def go():
            store = CryptoStateStore()
            await store.seed(["ethusdt"])
            await store.replace_candles("ethusdt", bars)
            return await store.get("ethusdt")
        return asyncio.run(go())

    def test_real_bars_give_the_volume_checks_something_to_read(self):
        state = self._install(self.bars())
        volumes = [c.volume for c in state.candles_1m]
        self.assertTrue(ind.has_usable_volume(volumes))
        self.assertIsNotNone(ind.relative_volume(volumes))

    def test_a_short_payload_cannot_wipe_a_working_history(self):
        import asyncio

        from analysis.crypto_state_store import CryptoStateStore

        async def go():
            store = CryptoStateStore()
            await store.seed(["ethusdt"])
            await store.replace_candles("ethusdt", self.bars())
            await store.replace_candles("ethusdt", self.bars(5))
            return await store.get("ethusdt")
        self.assertEqual(len(asyncio.run(go()).candles_1m), 60)

    def test_the_forming_bar_stays_open_for_the_next_poll_to_update(self):
        state = self._install(self.bars())
        self.assertFalse(state.candles_1m[-1].is_closed)
        self.assertTrue(all(c.is_closed for c in state.candles_1m[:-1]))

    def test_indicators_are_recomputed_from_the_installed_bars(self):
        state = self._install(self.bars())
        self.assertGreater(state.atr_14, 0)


class TestCandleParsing(unittest.TestCase):
    """
    The venue has published candles as objects and as positional arrays, with
    the time in seconds or milliseconds. A parser that insists on one shape
    turns a working feed into a silent outage.
    """

    def test_object_rows_parse(self):
        from collectors.coindcx import _parse_candles

        got = _parse_candles([{"time": 1755000000000, "open": 1, "high": 2,
                               "low": 0.5, "close": 1.5, "volume": 100}])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["volume"], 100.0)

    def test_positional_rows_parse(self):
        from collectors.coindcx import _parse_candles

        got = _parse_candles([[1755000000, 1, 2, 0.5, 1.5, 100]])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["close"], 1.5)

    def test_seconds_and_milliseconds_land_on_the_same_moment(self):
        """Guessing wrong puts every bar in 1970."""
        from collectors.coindcx import _parse_candles

        a = _parse_candles([[1755000000, 1, 2, 0.5, 1.5, 100]])
        b = _parse_candles([[1755000000000, 1, 2, 0.5, 1.5, 100]])
        self.assertEqual(a[0]["timestamp"], b[0]["timestamp"])
        self.assertGreater(a[0]["timestamp"].year, 2020)

    def test_rows_come_back_oldest_first(self):
        from collectors.coindcx import _parse_candles

        got = _parse_candles([[1755000120, 1, 2, 0.5, 1.5, 100],
                              [1755000000, 1, 2, 0.5, 1.5, 100]])
        self.assertEqual([r["timestamp"] for r in got],
                         sorted(r["timestamp"] for r in got))

    def test_junk_is_skipped_rather_than_raised(self):
        from collectors.coindcx import _parse_candles

        for junk in ({"error": "nope"}, [], None, "text",
                     [{"open": 1}], [[1, 2]], [{"time": "x", "open": 1, "close": 2}]):
            with self.subTest(junk=junk):
                self.assertEqual(_parse_candles(junk), [])

    def test_a_good_row_survives_a_bad_neighbour(self):
        from collectors.coindcx import _parse_candles

        got = _parse_candles([{"nonsense": True},
                              [1755000000, 1, 2, 0.5, 1.5, 100]])
        self.assertEqual(len(got), 1)

    def test_negative_volume_is_clamped_not_propagated(self):
        """A negative would poison has_usable_volume for the whole series."""
        from collectors.coindcx import _parse_candles

        got = _parse_candles([[1755000000, 1, 2, 0.5, 1.5, -5]])
        self.assertEqual(got[0]["volume"], 0.0)
