"""
Download real historical candles and backtest the crypto signal logic on them.

Run this anywhere with open network access — the Render shell, or your laptop.
It cannot run from the Claude sandbox, whose egress proxy blocks the market
data hosts by policy.

    python -m scripts.run_backtest --symbols BTCUSDT,ETHUSDT --days 30
    python -m scripts.run_backtest --symbols BTCUSDT --days 90 --leverage 10 --rr 2.0
    python -m scripts.run_backtest --sweep          # grid-search the parameters

Every run prints a scorecard and writes JSON to backtest_results/ so results
can be compared across parameter changes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime

from analysis.backtest import BacktestEngine
from analysis.paper_trading import CycleConfig, FeeModel
from collectors.historical_klines import HistoricalKlines

OUT_DIR = "backtest_results"


def _fmt_money(v: float) -> str:
    return f"{'+' if v >= 0 else '-'}Rs{abs(v):,.2f}"


def print_report(res, cfg: CycleConfig) -> None:
    s = res.summary()
    w, t, c, sig = s["wallet"], s["trades"], s["costs"], s["signals"]
    print("\n" + "=" * 74)
    print(f"  {res.symbol}   {s['period']['from'][:10]} -> {s['period']['to'][:10]}"
          f"   ({s['period']['candles']:,} candles)")
    print("=" * 74)

    print(f"  wallet        Rs{w['start']:,.2f} -> Rs{w['final']:,.2f}"
          f"   {_fmt_money(w['net_pnl'])}  ({w['return_pct']:+.1f}%)   [{w['ended_reason']}]")
    print(f"  trades        {t['total']}   win rate {t['win_rate_pct']}%"
          f"   realised R:R {t['realised_reward_risk']}   liquidations {t['liquidations']}")
    print(f"  per trade     avg win {_fmt_money(t['avg_win'])}"
          f"   avg loss {_fmt_money(t['avg_loss'])}"
          f"   expectancy {_fmt_money(t['expectancy_per_trade'])}")
    print(f"  risk          max drawdown {t['max_drawdown_pct']}%"
          f"   longest losing streak {t['longest_losing_streak']}")

    print(f"\n  gross P&L     {_fmt_money(c['gross_pnl'])}")
    print(f"  fees paid     -Rs{c['total_fees']:,.2f}", end="")
    if c["fees_as_pct_of_gross"] is not None:
        print(f"   ({c['fees_as_pct_of_gross']}% of gross profit)")
    else:
        print()
    print(f"  net P&L       {_fmt_money(w['net_pnl'])}")

    print(f"\n  target distance   median {sig['median_target_distance_pct']}%"
          f"   vs break-even {sig['break_even_move_pct']}%", end="")
    if sig["median_target_distance_pct"] <= sig["break_even_move_pct"]:
        print("   <-- TOO SMALL, fees exceed the target")
    else:
        ratio = sig["median_target_distance_pct"] / sig["break_even_move_pct"]
        print(f"   ({ratio:.1f}x cushion)")
    print(f"  signals           {sig['generated']} generated, {sig['taken']} taken, "
          f"{sig['rejected_low_confidence']} below confidence, "
          f"{sig['rejected_no_capital']} no capital")

    if s["by_signal_type"]:
        print("\n  by signal type:")
        print(f"    {'type':<22} {'trades':>7} {'win%':>7} {'net':>12} {'fees':>10}")
        for k, d in sorted(s["by_signal_type"].items(),
                           key=lambda kv: kv[1]["net"], reverse=True):
            print(f"    {k:<22} {d['trades']:>7} {d['win_rate']:>6.1f}% "
                  f"{_fmt_money(d['net']):>12} {d['fees']:>10,.2f}")


async def load(symbol: str, days: int, interval: str):
    hk = HistoricalKlines()
    print(f"  downloading {symbol} {interval} candles for the last {days}d ...", flush=True)
    candles = await hk.fetch_range(symbol, days=days, interval=interval)
    if not candles:
        print(f"  !! no data for {symbol}. Sources tried:")
        for e in hk.errors[:6]:
            print(f"       {e}")
        return []
    ranges = [c.true_range_pct for c in candles]
    ranges.sort()
    med = ranges[len(ranges) // 2] if ranges else 0.0
    print(f"  got {len(candles):,} candles via {hk.source_used}"
          f"   median intrabar range {med:.3f}%")
    if med == 0.0:
        print("  !! WARNING: candles are flat (high==low). ATR will collapse and"
              " every target will be far too small.")
    return candles


async def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest crypto signals on real candles")
    ap.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--interval", default="1m")
    ap.add_argument("--wallet", type=float, default=1000.0)
    ap.add_argument("--target", type=float, default=20000.0)
    ap.add_argument("--leverage", type=float, default=10.0)
    ap.add_argument("--rr", type=float, default=2.0, help="reward:risk ratio")
    ap.add_argument("--stop", type=float, default=0.20, help="stop as fraction of margin")
    ap.add_argument("--confidence", type=float, default=0.70)
    ap.add_argument("--margin-pct", type=float, default=0.20)
    ap.add_argument("--taker", type=float, default=0.00075)
    ap.add_argument("--sweep", action="store_true", help="grid-search leverage / R:R / confidence")
    args = ap.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    os.makedirs(OUT_DIR, exist_ok=True)

    data = {}
    for sym in symbols:
        candles = await load(sym, args.days, args.interval)
        if candles:
            data[sym] = candles
    if not data:
        print("\nNo data downloaded — cannot backtest. Check network access to "
              "data.binance.vision / api.binance.com / public.coindcx.com")
        return

    def build(lev, rr, conf):
        return CycleConfig(
            starting_wallet=args.wallet, target_wallet=args.target,
            leverage=lev, margin_per_trade_pct=args.margin_pct,
            stop_pct_of_margin=args.stop, reward_risk=rr,
            min_confidence=conf, fees=FeeModel(taker_pct=args.taker),
        )

    if args.sweep:
        print("\n" + "=" * 74)
        print("  PARAMETER SWEEP — net P&L summed across all symbols")
        print("=" * 74)
        print(f"  {'lev':>5} {'R:R':>5} {'conf':>6} | {'net P&L':>12} {'trades':>7} "
              f"{'win%':>7} {'fees':>10}")
        print("  " + "-" * 68)
        rows = []
        for lev in (5, 10, 20):
            for rr in (1.5, 2.0, 3.0):
                for conf in (0.65, 0.70, 0.75):
                    cfg = build(lev, rr, conf)
                    net = fees = trades = wins = 0.0
                    for sym, candles in data.items():
                        r = BacktestEngine(cfg).run(sym, candles)
                        net += r.net_pnl
                        fees += r.total_fees
                        trades += len(r.trades)
                        wins += r.wins
                    wr = wins / trades * 100 if trades else 0.0
                    rows.append((net, lev, rr, conf, trades, wr, fees))
                    print(f"  {lev:>4.0f}X {rr:>5.1f} {conf:>6.2f} | {_fmt_money(net):>12} "
                          f"{trades:>7.0f} {wr:>6.1f}% {fees:>10,.0f}")
        rows.sort(reverse=True)
        best = rows[0]
        print("\n  best combination: "
              f"{best[1]:.0f}X, R:R {best[2]}, confidence {best[3]} -> {_fmt_money(best[0])}")
        if best[0] <= 0:
            print("  NOTE: no combination was profitable. That is a real result, not a bug —")
            print("        it means the signal logic has no edge on this data after fees.")
        return

    cfg = build(args.leverage, args.rr, args.confidence)
    print("\n  config sanity:", json.dumps(cfg.sanity_report()))

    all_out = {}
    for sym, candles in data.items():
        res = BacktestEngine(cfg).run(sym, candles)
        print_report(res, cfg)
        all_out[sym] = res.summary()

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = os.path.join(OUT_DIR, f"backtest-{stamp}.json")
    with open(path, "w") as f:
        json.dump({"config": cfg.sanity_report(), "args": vars(args),
                   "results": all_out}, f, indent=2)
    print(f"\n  saved -> {path}\n")


if __name__ == "__main__":
    asyncio.run(main())
