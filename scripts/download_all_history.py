"""
Bulk historical data downloader (2017 to Present).

Downloads complete historical OHLCV candles from the Binance Public Vision Archive
(data.binance.vision) and saves them as compressed GZIP CSV files under data/historical/.

Zero impact on your PostgreSQL / SQLite database:
  - 100% free, zero API key required, no rate limit.
  - Standard gzip compression (~10x ratio) with zero C-DLL dependency issues.

Usage:
    python -m scripts.download_all_history --symbols BTCUSDT,ETHUSDT,SOLUSDT --intervals 15m,1h,4h,1d --start-year 2018
    python -m scripts.download_all_history --symbols BTCUSDT --intervals 1m --start-year 2023
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import io
import os
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pandas as pd
import structlog

log = structlog.get_logger()

VISION_BASE = "https://data.binance.vision/data/spot"
DATA_DIR = Path("data/historical")


async def download_month(client: httpx.AsyncClient, symbol: str, interval: str, year: int, month: int) -> pd.DataFrame | None:
    sym = symbol.upper()
    url = f"{VISION_BASE}/monthly/klines/{sym}/{interval}/{sym}-{interval}-{year:04d}-{month:02d}.zip"
    try:
        r = await client.get(url)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            log.warning("archive_bad_status", status=r.status_code, url=url)
            return None

        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            name = z.namelist()[0]
            text = z.read(name).decode("utf-8", errors="replace")

        rows = list(csv.reader(io.StringIO(text)))
        if rows and rows[0] and not rows[0][0].strip().lstrip("-").isdigit():
            rows = rows[1:]

        records = []
        for r in rows:
            try:
                ts_ms = int(float(r[0]))
                if ts_ms > 10**14:
                    ts_ms //= 1000
                records.append({
                    "timestamp": pd.to_datetime(ts_ms, unit="ms", utc=True),
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4]),
                    "volume": float(r[5]),
                })
            except (ValueError, IndexError):
                continue

        if not records:
            return None
        return pd.DataFrame(records)
    except Exception as exc:
        log.warning("download_failed", symbol=symbol, interval=interval, year=year, month=month, error=str(exc))
        return None


async def process_symbol_interval(symbol: str, interval: str, start_year: int, end_year: int) -> None:
    sym = symbol.upper()
    out_dir = DATA_DIR / sym / interval
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[*] Downloading {sym} ({interval}) from {start_year} to {end_year}...")
    now = datetime.now(UTC)

    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        for year in range(start_year, end_year + 1):
            year_file = out_dir / f"{year}.csv.gz"
            if year_file.exists() and year < now.year:
                print(f"    - {year} already cached: {year_file}")
                continue

            year_dfs = []
            max_month = now.month if year == now.year else 12
            for month in range(1, max_month + 1):
                df = await download_month(client, sym, interval, year, month)
                if df is not None and not df.empty:
                    year_dfs.append(df)

            if year_dfs:
                combined = pd.concat(year_dfs, ignore_index=True)
                combined.drop_duplicates(subset=["timestamp"], inplace=True)
                combined.sort_values(by="timestamp", inplace=True)
                combined.to_csv(year_file, index=False, compression="gzip")
                print(f"    + Saved {year}: {len(combined):,} candles -> {year_file}")
            else:
                print(f"    . No data available for {year}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Download historical Binance Kline data to compressed CSV")
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,BNBUSDT", help="Comma-separated symbols")
    parser.add_argument("--intervals", default="15m,30m,1h,4h,1d", help="Comma-separated intervals (1m, 15m, 30m, 1h, 4h, 1d)")
    parser.add_argument("--start-year", type=int, default=2018, help="Start year (e.g. 2017 or 2018)")
    parser.add_argument("--end-year", type=int, default=datetime.now(UTC).year, help="End year")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    intervals = [i.strip().lower() for i in args.intervals.split(",") if i.strip()]

    print("=" * 65)
    print("  Historical OHLCV Downloader (Binance Vision -> Gzip CSV)")
    print(f"  Symbols:   {', '.join(symbols)}")
    print(f"  Intervals: {', '.join(intervals)}")
    print(f"  Years:     {args.start_year} -> {args.end_year}")
    print(f"  Storage:   {DATA_DIR.resolve()}")
    print("=" * 65)

    for sym in symbols:
        for interval in intervals:
            await process_symbol_interval(sym, interval, args.start_year, args.end_year)

    print("\n[OK] Historical data download completed.")


if __name__ == "__main__":
    asyncio.run(main())
