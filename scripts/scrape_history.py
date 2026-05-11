"""
One-shot historical data scraper.

Run this on your local machine to bulk-load all Sackmann data into PostgreSQL.
Takes ~15-30 minutes depending on internet speed.

    python -m scripts.scrape_history

Expected output:
    ~240k match records (ATP + WTA, 2000-2024)
    ~2.5M slam points (all 4 slams, 2011-2024)
    ~8k player_stats rows (per-surface aggregates)

Disk usage: ~500MB in PostgreSQL.

Set DATABASE_URL in .env or environment before running.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

import storage.models  # noqa: F401 — register all models before create_all
from collectors.historical_importer import run_import
from collectors.slam_pbp_importer import run_slam_import
from storage.database import AsyncSessionFactory, init_db

log = structlog.get_logger()


async def main() -> None:
    print("=" * 60)
    print("Tennis-bet Historical Data Scraper")
    print("=" * 60)
    print()

    print("[1/3] Initialising database tables...")
    await init_db()
    print("      Done.")
    print()

    print("[2/3] Downloading ATP/WTA match records (2000-2024)...")
    print("      ~240k matches, ~50MB download, ~5-10 min")
    t0 = time.time()
    async with AsyncSessionFactory() as session:
        await run_import(session, force=False)
    elapsed = time.time() - t0
    print(f"      Done in {elapsed:.0f}s.")
    print()

    print("[3/3] Downloading Grand Slam point-by-point data (2011-2024)...")
    print("      ~2.5M points, ~200MB download, ~15-20 min")
    t0 = time.time()
    async with AsyncSessionFactory() as session:
        await run_slam_import(session, force=False)
    elapsed = time.time() - t0
    print(f"      Done in {elapsed:.0f}s.")
    print()

    print("=" * 60)
    print("All historical data loaded successfully.")
    print("You can now run the backtest:")
    print("  python -m scripts.backtest --year 2023")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
