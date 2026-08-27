"""
Historical Data Service.

Reads, preloads, and manages historical OHLCV data directly from compressed
repository files (data/historical/*.csv.gz), eliminating external database dependencies
for model training, indicator warm-up, and multi-timeframe analysis.
"""
from __future__ import annotations

import asyncio
import csv
import gzip
import io
import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import structlog

from analysis.crypto_state import CryptoState, OHLCVCandle
from analysis.crypto_state_store import CryptoStateStore, append_candle, recalculate_indicators

log = structlog.get_logger()

DATA_DIR = Path("data/historical")
VISION_BASE = "https://data.binance.vision/data/spot"


class HistoricalDataService:
    """Provides fast, zero-database access to bundled historical candle datasets."""

    @staticmethod
    def list_available_symbols() -> list[str]:
        if not DATA_DIR.exists():
            return []
        return [p.name.lower() for p in DATA_DIR.iterdir() if p.is_dir()]

    @staticmethod
    def load_candles(
        symbol: str,
        interval: str = "1h",
        limit: int | None = None,
    ) -> list[OHLCVCandle]:
        """
        Load historical candles for a symbol from compressed local files.
        Sorted chronologically.
        """
        sym_dir = DATA_DIR / symbol.upper() / interval
        if not sym_dir.exists():
            # Try lowercase directory name
            sym_dir = DATA_DIR / symbol.lower() / interval
            if not sym_dir.exists():
                return []

        files = sorted(sym_dir.glob("*.csv.gz"))
        if not files:
            files = sorted(sym_dir.glob("*.csv"))
        if not files:
            return []

        candles: list[OHLCVCandle] = []
        for file_path in files:
            try:
                if file_path.suffix == ".gz":
                    with gzip.open(file_path, "rt", encoding="utf-8", errors="replace") as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            ts_val = row.get("timestamp") or row.get("time") or row.get("ts")
                            if not ts_val:
                                continue
                            try:
                                if ts_val.isdigit():
                                    ts_int = int(ts_val)
                                    if ts_int > 10**14:
                                        ts_int //= 1000
                                    ts = datetime.fromtimestamp(ts_int / 1000, tz=UTC)
                                else:
                                    ts = datetime.fromisoformat(ts_val.replace("Z", "+00:00"))
                                candles.append(OHLCVCandle(
                                    open=float(row["open"]),
                                    high=float(row["high"]),
                                    low=float(row["low"]),
                                    close=float(row["close"]),
                                    volume=float(row.get("volume", 0.0)),
                                    timestamp=ts,
                                    is_closed=True,
                                ))
                            except (ValueError, TypeError, KeyError):
                                continue
                else:
                    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                        reader = csv.DictReader(f)
                        for row in reader:
                            try:
                                ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                                candles.append(OHLCVCandle(
                                    open=float(row["open"]),
                                    high=float(row["high"]),
                                    low=float(row["low"]),
                                    close=float(row["close"]),
                                    volume=float(row.get("volume", 0.0)),
                                    timestamp=ts,
                                    is_closed=True,
                                ))
                            except (ValueError, TypeError, KeyError):
                                continue
            except Exception as exc:
                log.warning("historical_file_read_error", file=str(file_path), error=str(exc))

        candles.sort(key=lambda c: c.timestamp)
        if limit is not None and limit > 0:
            return candles[-limit:]
        return candles

    @classmethod
    async def preload_states(cls, store: CryptoStateStore, limit: int = 360) -> int:
        """
        Preload initial candle history into in-memory CryptoStateStore on startup.
        Provides instant indicator warm-up without waiting for live polls.
        """
        symbols = cls.list_available_symbols()
        if not symbols:
            return 0

        loaded_count = 0
        for sym in symbols:
            candles = cls.load_candles(sym, interval="1m", limit=limit)
            if not candles:
                candles = cls.load_candles(sym, interval="15m", limit=limit)
            if not candles:
                candles = cls.load_candles(sym, interval="1h", limit=limit)

            if not candles:
                continue

            state = await store.get(sym)
            if state is None:
                await store.seed([sym])
                state = await store.get(sym)

            if state is None:
                continue

            for c in candles:
                append_candle(state, c)
            if state.candles_1m:
                state.current_price = state.candles_1m[-1].close
                state.timestamp = state.candles_1m[-1].timestamp
                recalculate_indicators(state)
            loaded_count += 1

        if loaded_count > 0:
            log.info("historical_states_preloaded", symbols_loaded=loaded_count)
        return loaded_count

    @classmethod
    async def download_symbol_history(
        cls,
        symbol: str,
        interval: str,
        start_year: int,
        end_year: int,
    ) -> int:
        """Download and package historical data directly to data/historical."""
        sym = symbol.upper()
        out_dir = DATA_DIR / sym / interval
        out_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        total_saved = 0

        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            for year in range(start_year, end_year + 1):
                year_file = out_dir / f"{year}.csv.gz"
                if year_file.exists() and year < now.year:
                    continue

                records: list[dict] = []
                max_month = now.month if year == now.year else 12
                for month in range(1, max_month + 1):
                    url = f"{VISION_BASE}/monthly/klines/{sym}/{interval}/{sym}-{interval}-{year:04d}-{month:02d}.zip"
                    try:
                        r = await client.get(url)
                        if r.status_code != 200:
                            continue
                        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                            name = z.namelist()[0]
                            text = z.read(name).decode("utf-8", errors="replace")
                        rows = list(csv.reader(io.StringIO(text)))
                        if rows and rows[0] and not rows[0][0].strip().lstrip("-").isdigit():
                            rows = rows[1:]
                        for row in rows:
                            try:
                                ts_ms = int(float(row[0]))
                                if ts_ms > 10**14:
                                    ts_ms //= 1000
                                dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
                                records.append({
                                    "timestamp": dt.isoformat(),
                                    "open": float(row[1]),
                                    "high": float(row[2]),
                                    "low": float(row[3]),
                                    "close": float(row[4]),
                                    "volume": float(row[5]),
                                })
                            except (ValueError, IndexError):
                                continue
                    except Exception:
                        continue

                if records:
                    records.sort(key=lambda x: x["timestamp"])
                    seen = set()
                    deduped = []
                    for rec in records:
                        if rec["timestamp"] not in seen:
                            seen.add(rec["timestamp"])
                            deduped.append(rec)

                    with gzip.open(year_file, "wt", encoding="utf-8", newline="") as gz_out:
                        writer = csv.DictWriter(gz_out, fieldnames=["timestamp", "open", "high", "low", "close", "volume"])
                        writer.writeheader()
                        writer.writerows(deduped)
                    total_saved += len(deduped)

        return total_saved
