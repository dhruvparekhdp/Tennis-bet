"""
Confluence: the claim is that agreement beats accumulation.

The tests that matter here are directional accuracy on data with a known
answer, and selectivity — finding less to trade in noise than in a trend.
Adding analyzers is easy; showing they made the answer better is the part
that counts.
"""
import random
import unittest
from datetime import UTC, datetime, timedelta

from analysis.confluence import (
    MIN_AGREEING_FAMILIES,
    Family,
    evaluate,
    volatility_veto,
)
from analysis.crypto_state import OHLCVCandle

T0 = datetime(2026, 8, 18, tzinfo=UTC)


def series(n, seed, drift=0.0, vol=0.005, start=100.0):
    random.seed(seed)
    out, p = [], start
    for i in range(n):
        o = p
        p = o * (1 + drift + random.gauss(0, vol))
        w = abs(random.gauss(0, vol * 0.5)) * p
        out.append(OHLCVCandle(o, max(o, p) + w, min(o, p) - w, p,
                               random.uniform(80, 400), T0 + timedelta(minutes=i)))
    return out


class TestDirectionalAccuracy(unittest.TestCase):
    """The only claim worth making: when it does speak, is it right."""

    def _verdicts(self, drift, seeds=range(60)):
        out = []
        for s in seeds:
            v = evaluate(series(220, s, drift=drift, vol=0.005))
            if v.direction:
                out.append(v)
        return out

    def test_it_goes_long_in_an_uptrend_and_never_short(self):
        vs = self._verdicts(0.0015)
        self.assertGreater(len(vs), 0, "should find something in a clear trend")
        self.assertTrue(all(v.direction == "long" for v in vs),
                        [v.direction for v in vs])

    def test_it_goes_short_in_a_downtrend_and_never_long(self):
        vs = self._verdicts(-0.0015)
        self.assertGreater(len(vs), 0)
        self.assertTrue(all(v.direction == "short" for v in vs),
                        [v.direction for v in vs])

    def test_it_finds_less_to_trade_in_noise_than_in_a_trend(self):
        trend = len(self._verdicts(0.0015))
        noise = len(self._verdicts(0.0))
        self.assertLessEqual(noise, trend)


class TestAgreementRules(unittest.TestCase):
    def test_no_verdict_without_enough_agreeing_families(self):
        for _ in range(40):
            v = evaluate(series(220, 77, drift=0.0))
            if v.direction is None and v.vetoes:
                continue
            self.assertGreaterEqual(v.agreeing_families, MIN_AGREEING_FAMILIES)

    def test_confidence_never_reaches_certainty(self):
        for s in range(40):
            v = evaluate(series(220, s, drift=0.002))
            self.assertLessEqual(v.confidence, 0.92)

    def test_dissent_lowers_confidence(self):
        """Eight agreeing with two against must be worth less than eight with none."""
        found = [evaluate(series(220, s, drift=0.0015)) for s in range(60)]
        clean = [v for v in found if v.direction and v.dissenting_families == 0]
        split = [v for v in found if v.direction and v.dissenting_families > 0]
        if clean and split:
            self.assertGreater(sum(v.confidence for v in clean) / len(clean),
                               sum(v.confidence for v in split) / len(split))

    def test_volatility_is_a_veto_not_a_vote(self):
        """A quiet market picks no side — it is a reason not to trade."""
        v = evaluate(series(220, 5, drift=0.002, vol=0.0001))
        self.assertIsNone(v.direction)
        self.assertTrue(v.vetoes)
        self.assertNotIn(Family.VOLATILITY, [x.family for x in v.votes])

    def test_short_history_is_refused_rather_than_guessed(self):
        v = evaluate(series(30, 1, drift=0.002))
        self.assertIsNone(v.direction)
        self.assertIn("not enough history", v.vetoes)


class TestVolatilityVeto(unittest.TestCase):
    def _arrays(self, candles):
        return ([c.high for c in candles], [c.low for c in candles],
                [c.close for c in candles])

    def test_an_absolutely_quiet_market_is_vetoed(self):
        """
        The relative percentile alone let a 0.034% ATR market through, because
        it was volatile *for itself*. The absolute floor catches that.
        """
        h, low, c = self._arrays(series(220, 3, vol=0.0002))
        reason = volatility_veto(h, low, c)
        self.assertIsNotNone(reason)
        self.assertIn("costs", reason)

    def test_a_flat_feed_is_vetoed(self):
        h, low, c = self._arrays(series(220, 3, vol=0.000001))
        self.assertIsNotNone(volatility_veto(h, low, c))

    def test_a_normally_active_market_is_not_vetoed_on_the_absolute_test(self):
        h, low, c = self._arrays(series(220, 8, vol=0.006))
        reason = volatility_veto(h, low, c)
        if reason is not None:
            self.assertNotIn("costs", reason)


class TestFamiliesAreIndependent(unittest.TestCase):
    def test_momentum_indicators_produce_one_vote_not_three(self):
        """
        RSI, Stochastic and MACD are three views of the same thing. Counting
        them separately triple-counts one piece of evidence, which is how a
        system talks itself into confidence it has not earned.
        """
        v = evaluate(series(220, 2, drift=0.0015))
        families = [x.family for x in v.votes]
        self.assertEqual(len(families), len(set(families)))
        self.assertIn(Family.MOMENTUM, families)

    def test_every_family_is_represented_exactly_once(self):
        v = evaluate(series(220, 2, drift=0.0015))
        self.assertEqual(
            {x.family for x in v.votes},
            {Family.TREND, Family.MOMENTUM, Family.VOLUME,
             Family.STRUCTURE, Family.PATTERN})
