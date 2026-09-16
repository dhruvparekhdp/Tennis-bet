"""
Win-rate trend diagnostic, by day and by setup.

Why this exists
----------------
The accuracy dashboard shows the whole archive as one number. That hides two
things that matter more than the aggregate: whether the win rate is trending
down over time (rather than just being low), and whether one signal type is
dragging every other one down while hiding inside a blended total.

This groups crypto_signal_log by calendar day and by signal_type, and prints
both a day-by-day trend and a per-setup breakdown, so "the win rate is
degrading" can be confirmed or ruled out against the real data rather than
guessed at from a single dashboard number.

Usage
-----
Run wherever DATABASE_URL points at the real database (locally with an SSH
tunnel or a direct connection string, or in a one-off Render shell):

    DATABASE_URL=postgresql+asyncpg://... python scripts/win_rate_trend.py

With no DATABASE_URL set, it falls back to the local sqlite default, which is
only useful for testing this script itself, not for diagnosing production.

Only resolved signals (outcome in won/lost/expired) are counted toward a win
rate — pending signals are excluded rather than counted as losses, the same
convention signal_audit.py uses, because a young or paused signal is not a
losing one.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict

from sqlalchemy import select

from storage.database import AsyncSessionFactory
from storage.models import CryptoSignalLog

TERMINAL = ("won", "lost", "expired")


def _win_rate(rows: list[CryptoSignalLog]) -> tuple[float | None, int, int]:
    """(win_rate_pct, wins, decided). None if nothing has resolved yet."""
    decided = [r for r in rows if r.outcome in ("won", "lost")]
    if not decided:
        return None, 0, 0
    wins = sum(1 for r in decided if r.outcome == "won")
    return round(wins / len(decided) * 100, 1), wins, len(decided)


async def main() -> None:
    try:
        async with AsyncSessionFactory() as session:
            result = await session.execute(
                select(CryptoSignalLog).order_by(CryptoSignalLog.timestamp))
            rows = list(result.scalars())
    except Exception as exc:
        # Most likely cause: DATABASE_URL wasn't set, so this ran against a
        # fresh local sqlite file with no tables at all — not the same
        # failure as "connected fine, table is just empty" below.
        print(f"Could not read crypto_signal_log: {exc}\n"
              "Check DATABASE_URL is set to the real database.")
        return

    if not rows:
        print("No rows in crypto_signal_log. Check DATABASE_URL is pointed "
              "at the real database, not the local default.")
        return

    resolved = [r for r in rows if r.outcome in TERMINAL]
    print(f"{len(rows)} signals total, {len(resolved)} resolved, "
          f"{len(rows) - len(resolved)} pending.\n")

    # ── By day ────────────────────────────────────────────────────────────
    by_day: dict[str, list[CryptoSignalLog]] = defaultdict(list)
    for r in resolved:
        by_day[r.timestamp.strftime("%Y-%m-%d")].append(r)

    print(f"{'date':<12} {'fired':>6} {'decided':>8} {'win %':>7}  "
          f"{'vol_spike share':>16} {'vol_spike win %':>16}")
    running_vs_share = []
    for day in sorted(by_day):
        day_rows = by_day[day]
        wr, wins, decided = _win_rate(day_rows)
        vs_rows = [r for r in day_rows if r.signal_type == "volume_spike"]
        vs_share = round(len(vs_rows) / len(day_rows) * 100, 1) if day_rows else 0.0
        vs_wr, _, vs_decided = _win_rate(vs_rows)
        running_vs_share.append(vs_share)
        wr_str = f"{wr}%" if wr is not None else "  —"
        vs_wr_str = f"{vs_wr}%" if vs_wr is not None else "  —"
        print(f"{day:<12} {len(day_rows):>6} {decided:>8} {wr_str:>7}  "
              f"{vs_share:>15.1f}% {vs_wr_str:>16}")

    if len(running_vs_share) >= 4:
        first_half = running_vs_share[:len(running_vs_share) // 2]
        second_half = running_vs_share[len(running_vs_share) // 2:]
        avg_first = sum(first_half) / len(first_half)
        avg_second = sum(second_half) / len(second_half)
        print(f"\nvolume_spike's share of daily signals: "
              f"{avg_first:.1f}% (first half of the archive) -> "
              f"{avg_second:.1f}% (second half)")
        if avg_second > avg_first + 5:
            print("Its share is growing. If it has a lower win rate than the "
                  "rest of the mix, a growing share alone would drag the "
                  "blended, all-time win rate down over time even with no "
                  "other change — this is what 'degrading day by day' looks "
                  "like from a blended number.")

    # ── By setup ──────────────────────────────────────────────────────────
    by_setup: dict[str, list[CryptoSignalLog]] = defaultdict(list)
    for r in resolved:
        by_setup[r.signal_type].append(r)

    print(f"\n{'setup':<18} {'fired':>6} {'decided':>8} {'win %':>7}  "
          f"{'share of archive':>17}")
    for setup in sorted(by_setup, key=lambda s: -len(by_setup[s])):
        setup_rows = by_setup[setup]
        wr, wins, decided = _win_rate(setup_rows)
        share = round(len(setup_rows) / len(resolved) * 100, 1)
        wr_str = f"{wr}%" if wr is not None else "  —"
        print(f"{setup:<18} {len(setup_rows):>6} {decided:>8} {wr_str:>7}  "
              f"{share:>16.1f}%")

    print("\nA setup's own break-even win rate depends on its reward:risk, "
          "not on 50%. See analysis/scalp_levels.py's atr_target_multiple "
          "per analyzer and PORT_NOTES.md/README.md's cost-model section for "
          "the break-even-vs-R:R table — a setup demanding a 3:1 move needs "
          "roughly 30% just to break even, not 50%.")


if __name__ == "__main__":
    asyncio.run(main())
