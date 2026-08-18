"""
Historical OHLC candle fetcher for the backtest engine.

Sources, tried in order:

1. data.binance.vision — Binance's public archive. Free, no key, no rate limit,
   serves whole months of 1m candles as a single zipped CSV. This is the bulk
   backfill path and by far the fastest way to get years of history.
2. Binance REST klines — up to 1000 candles per call. Used to top up recent
   data the archive has not published yet.
3. CoinDCX candles — fallback when Binance is geo-blocked from the host.

Why this matters beyond convenience: these are REAL candles with a true high
and low. The live REST pollers can only produce flat candles where
open == high == low == close, which collapses ATR to roughly the price drift
between two polls and is the reason current signal targets are ~0.05% wide.
Backtesting on real candles is therefore also the fix for target sizing.
"""
from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import httpx
import structlog

log = structlog.get_logger()

VISION_BASE = "https://data.binance.vision/data/spot"
BINANCE_API = "https://api.binance.com/api/v3/klines"
COINDCX_CANDLES = "https://public.coindcx.com/market_data/candles"


@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def true_range_pct(self) -> float:
        """Intrabar range as % of close — the thing flat synthetic candles lose."""
        return (self.high - self.low) / self.close * 100.0 if self.close else 0.0


def _rows_to_candles(rows) -> list[Candle]:
    out: list[Candle] = []
    for r in rows:
        try:
            # Binance kline layout: openTime, O, H, L, C, V, closeTime, ...
            ts_ms = int(float(r[0]))
            # archives switched to microseconds for recent months
            if ts_ms > 10**14:
                ts_ms //= 1000
            out.append(Candle(
                ts=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
                open=float(r[1]), high=float(r[2]), low=float(r[3]),
                close=float(r[4]), volume=float(r[5]),
            ))
        except (ValueError, IndexError, TypeError):
            continue
    return out


class HistoricalKlines:
    """Fetches OHLC candles from whichever public source is reachable."""

    def __init__(self, timeout: float = 60.0) -> None:
        self.timeout = timeout
        self.source_used: str | None = None
        self.errors: list[str] = []

    # ── Binance monthly archive (bulk) ────────────────────────────────────

    async def fetch_month(self, symbol: str, year: int, month: int,
                          interval: str = "1m") -> list[Candle]:
        sym = symbol.upper()
        url = (f"{VISION_BASE}/monthly/klines/{sym}/{interval}/"
               f"{sym}-{interval}-{year:04d}-{month:02d}.zip")
        return await self._fetch_zip(url, f"vision monthly {year}-{month:02d}")

    async def fetch_day(self, symbol: str, day: date,
                        interval: str = "1m") -> list[Candle]:
        sym = symbol.upper()
        url = (f"{VISION_BASE}/daily/klines/{sym}/{interval}/"
               f"{sym}-{interval}-{day.isoformat()}.zip")
        return await self._fetch_zip(url, f"vision daily {day}")

    async def _fetch_zip(self, url: str, label: str) -> list[Candle]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as c:
                r = await c.get(url)
            if r.status_code == 404:
                log.info("klines_archive_missing", label=label)
                return []
            if r.status_code != 200:
                self.errors.append(f"{label}: HTTP {r.status_code}")
                return []
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                name = z.namelist()[0]
                text = z.read(name).decode("utf-8", errors="replace")
            rows = list(csv.reader(io.StringIO(text)))
            # newer archives ship a header row
            if rows and rows[0] and not rows[0][0].strip().lstrip("-").isdigit():
                rows = rows[1:]
            candles = _rows_to_candles(rows)
            if candles:
                self.source_used = "binance_vision"
            log.info("klines_archive_loaded", label=label, candles=len(candles))
            return candles
        except Exception as exc:
            self.errors.append(f"{label}: {type(exc).__name__} {exc}")
            log.warning("klines_archive_failed", label=label, error=str(exc)[:160])
            return []

    # ── Recent data ───────────────────────────────────────────────────────

    async def fetch_recent(self, symbol: str, interval: str = "1m",
                           limit: int = 1000) -> list[Candle]:
        """Most recent `limit` candles. Tries Binance REST, then CoinDCX."""
        sym = symbol.upper()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.get(BINANCE_API, params={
                    "symbol": sym, "interval": interval, "limit": min(limit, 1000)})
            if r.status_code == 200:
                candles = _rows_to_candles(r.json())
                if candles:
                    self.source_used = "binance_api"
                    return candles
            self.errors.append(f"binance api: HTTP {r.status_code}")
        except Exception as exc:
            self.errors.append(f"binance api: {type(exc).__name__} {exc}")

        # CoinDCX fallback — pair format is B-BTC_USDT
        base = sym[:-4] if sym.endswith("USDT") else sym
        pair = f"B-{base}_USDT"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.get(COINDCX_CANDLES, params={
                    "pair": pair, "interval": interval, "limit": min(limit, 1000)})
            if r.status_code == 200:
                out = []
                for row in r.json():
                    try:
                        out.append(Candle(
                            ts=datetime.fromtimestamp(int(row["time"]) / 1000, tz=UTC),
                            open=float(row["open"]), high=float(row["high"]),
                            low=float(row["low"]), close=float(row["close"]),
                            volume=float(row.get("volume", 0.0)),
                        ))
                    except (KeyError, ValueError, TypeError):
                        continue
                out.sort(key=lambda c: c.ts)
                if out:
                    self.source_used = "coindcx"
                    return out
            self.errors.append(f"coindcx: HTTP {r.status_code}")
        except Exception as exc:
            self.errors.append(f"coindcx: {type(exc).__name__} {exc}")

        return []

    async def fetch_range(self, symbol: str, days: int,
                          interval: str = "1m") -> list[Candle]:
        """
        Candles covering roughly the last `days` days.

        Pulls whole months from the archive where possible, then tops up with
        recent REST data. Deduplicates and sorts, so overlapping sources are safe.
        """
        end = datetime.now(UTC).date()
        start = end - timedelta(days=days)
        seen: dict[datetime, Candle] = {}

        month = date(start.year, start.month, 1)
        while month <= end:
            for c in await self.fetch_month(symbol, month.year, month.month, interval):
                seen[c.ts] = c
            month = date(month.year + (month.month // 12), (month.month % 12) + 1, 1)

        # recent days the monthly archive has not published yet
        day = max(start, date(end.year, end.month, 1))
        while day <= end:
            for c in await self.fetch_day(symbol, day, interval):
                seen[c.ts] = c
            day += timedelta(days=1)

        if not seen:
            for c in await self.fetch_recent(symbol, interval, 1000):
                seen[c.ts] = c

        cutoff = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
        candles = sorted((c for c in seen.values() if c.ts >= cutoff), key=lambda c: c.ts)
        log.info("klines_range_ready", symbol=symbol, days=days,
                 candles=len(candles), source=self.source_used)
        return candles
