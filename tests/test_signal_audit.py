"""
The audit page is where a wrong number is most dangerous — it is the screen
used to decide whether the strategy works, so a flattering bug here would be
believed. These tests pin the arithmetic that makes gross and net disagree.
"""
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta

from analysis.scalp_levels import ScalpConfig
from analysis.signal_audit import STAGES, audit, method_catalogue, to_verdict


@dataclass
class Row:
    """Stands in for a CryptoSignalLog row — same attribute names, no ORM."""

    id: int = 1
    symbol: str = "ethusdt"
    signal_type: str = "confluence"
    direction: str = "long"
    timeframe: str = "30m"
    confidence: float = 0.8
    current_price: float = 1000.0
    target_price: float = 1005.0
    stop_loss: float = 995.0
    outcome: str = "won"
    pnl_pct: float = 0.5
    timestamp: datetime = datetime(2026, 8, 20, 12, 0)


class TestVerdictArithmetic(unittest.TestCase):
    def test_move_risk_and_reward_risk_come_from_the_levels(self):
        v = to_verdict(Row())
        self.assertAlmostEqual(v.move_pct, 0.5, places=6)
        self.assertAlmostEqual(v.risk_pct, 0.5, places=6)
        self.assertAlmostEqual(v.reward_risk, 1.0, places=6)

    def test_cost_is_taken_per_market_not_one_global_rate(self):
        """Gold's brokerage is a fifth of ether's, and the audit must say so."""
        eth = to_verdict(Row(symbol="ethusdt"))
        xau = to_verdict(Row(symbol="xauusdt"))
        self.assertLess(xau.cost_pct, eth.cost_pct)

    def test_a_win_smaller_than_its_costs_is_not_a_net_win(self):
        """The failure this page exists to surface: green on gross, red on net."""
        row = Row(current_price=1000.0, target_price=1001.0, stop_loss=999.0,
                  outcome="won", pnl_pct=0.1)
        v = to_verdict(row)
        self.assertTrue(v.won)
        self.assertLess(v.net_pnl_pct, 0)
        self.assertFalse(v.net_won)

    def test_a_win_that_clears_its_costs_is_a_net_win(self):
        v = to_verdict(Row(outcome="won", pnl_pct=0.5))
        self.assertTrue(v.net_won)
        self.assertAlmostEqual(v.net_pnl_pct, 0.5 - v.cost_pct, places=9)

    def test_a_pending_signal_has_no_net_result_yet(self):
        v = to_verdict(Row(outcome="pending", pnl_pct=0.0))
        self.assertEqual(v.net_pnl_pct, 0.0)
        self.assertFalse(v.net_won)
        self.assertFalse(v.decided)

    def test_x_cost_below_one_means_it_loses_when_it_wins(self):
        v = to_verdict(Row(current_price=1000.0, target_price=1000.5, stop_loss=999.5))
        self.assertLess(v.x_cost, 1.0)


class TestAggregation(unittest.TestCase):
    def rows(self):
        base = datetime(2026, 8, 20, 12, 0)
        return [
            Row(id=1, symbol="bchusdt", timeframe="15m", outcome="won", pnl_pct=0.6,
                current_price=100.0, target_price=100.6, stop_loss=99.4, timestamp=base),
            Row(id=2, symbol="bchusdt", timeframe="15m", outcome="lost", pnl_pct=-0.6,
                current_price=100.0, target_price=100.6, stop_loss=99.4,
                timestamp=base + timedelta(hours=1)),
            Row(id=3, symbol="ethusdt", timeframe="30m", outcome="pending", pnl_pct=0.0,
                timestamp=base + timedelta(hours=2)),
        ]

    def test_pending_signals_never_count_as_losses(self):
        rep = audit(self.rows())
        t = rep["totals"]
        self.assertEqual((t["n"], t["decided"], t["pending"]), (3, 2, 1))
        self.assertEqual(t["hit_rate_pct"], 50.0)

    def test_a_slice_with_nothing_resolved_reports_null_not_zero(self):
        """An untested slice is not a losing slice, and must not read as one."""
        rep = audit([Row(outcome="pending", pnl_pct=0.0)])
        self.assertIsNone(rep["totals"]["hit_rate_pct"])
        self.assertIsNone(rep["totals"]["expectancy_pct"])

    def test_expectancy_is_net_of_cost_so_a_coin_flip_reads_negative(self):
        """
        Symmetric wins and losses at 1:1 are break-even on gross and a loss
        after the round trip. If this ever reads positive the page is lying.
        """
        rep = audit(self.rows())
        self.assertEqual(rep["totals"]["hit_rate_pct"], 50.0)
        self.assertLess(rep["totals"]["expectancy_pct"], 0)

    def test_every_slice_covers_the_same_signals(self):
        rep = audit(self.rows())
        for key in ("by_symbol", "by_setup", "by_horizon", "by_direction",
                    "by_confidence", "by_edge"):
            with self.subTest(slice=key):
                self.assertEqual(sum(b["n"] for b in rep[key]), rep["totals"]["n"])

    def test_slices_are_ordered_by_evidence(self):
        rep = audit(self.rows())
        counts = [b["n"] for b in rep["by_symbol"]]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_records_carry_the_horizon_label(self):
        rep = audit(self.rows())
        self.assertEqual({r["horizon"] for r in rep["records"]}, {"15m", "30m"})

    def test_an_empty_window_produces_a_report_rather_than_an_error(self):
        rep = audit([])
        self.assertEqual(rep["totals"]["n"], 0)
        self.assertEqual(rep["records"], [])

    def test_execution_costs_are_overridable_but_brokerage_is_not(self):
        """
        Spread and slippage are assumptions and follow the config. The
        brokerage is a fact about the market, so `for_symbol` reimposes it —
        an audit that let a caller zero the fee could show any strategy as
        profitable.
        """
        cheap = ScalpConfig(round_trip_fee_pct=0.0, spread_pct=0.0,
                            slippage_buffer_pct=0.0)
        rep = audit([Row(outcome="won", pnl_pct=0.5)], cfg=cheap)
        rec = rep["records"][0]
        self.assertGreater(rec["cost_pct"], 0)
        self.assertLess(rec["cost_pct"], to_verdict(Row()).cost_pct)
        self.assertAlmostEqual(rec["net_pnl_pct"],
                               round(rec["pnl_pct"] - rec["cost_pct"], 4), places=4)


class TestMethodCatalogue(unittest.TestCase):
    def test_every_declared_method_resolves(self):
        """
        The catalogue is introspected, so a renamed or deleted function fails
        here rather than becoming a wrong description on a live page.
        """
        cat = method_catalogue()
        self.assertEqual(len(cat), len(STAGES))
        for stage in cat:
            with self.subTest(stage=stage["stage"]):
                self.assertTrue(stage["methods"])
                self.assertEqual(stage["count"], len(stage["methods"]))

    def test_signatures_and_source_lines_are_real(self):
        for stage in method_catalogue():
            for m in stage["methods"]:
                with self.subTest(method=m["name"]):
                    self.assertTrue(m["signature"].startswith(m["name"]))
                    path, _, line = m["source"].rpartition(":")
                    self.assertTrue(path.endswith(".py"))
                    self.assertGreater(int(line), 0)

    def test_the_summary_is_one_line(self):
        for stage in method_catalogue():
            for m in stage["methods"]:
                self.assertNotIn("\n", m["summary"])

    def test_classes_list_their_public_members(self):
        by_name = {m["name"]: m for st in method_catalogue() for m in st["methods"]}
        self.assertEqual(by_name["ScalpConfig"]["kind"], "class")
        self.assertIn("for_symbol", by_name["ScalpConfig"]["members"])

    def test_no_method_is_listed_twice(self):
        seen = [(m["module"], m["name"])
                for st in method_catalogue() for m in st["methods"]]
        self.assertEqual(len(seen), len(set(seen)))


if __name__ == "__main__":
    unittest.main()
