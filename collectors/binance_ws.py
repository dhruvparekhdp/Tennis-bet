from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import structlog
import websockets

from analysis.crypto_state_store import CryptoStateStore

log = structlog.get_logger()


class BinanceWSCollector:
    """
    Real-time Binance WebSocket collector streaming Klines & 24h stats for
    every symbol in the DB-backed watchlist (managed from the dashboard's
    Crypto tab, not an env var).
    """

    BASE_WS_URL = "wss://stream.binance.com:9443/stream"
    _LARGE_WATCHLIST_WARN = 25  # Render free tier has limited CPU/RAM — flag heavy watchlists

    def __init__(self, store: CryptoStateStore) -> None:
        self.store = store
        self._running = False
        self._consecutive_failures = 0
        self._total_messages_received = 0
        self._last_message_time: datetime | None = None
        self._subscribed_version = -1
        self._kline_interval = "1m"

    async def run_forever(self) -> None:
        """Continuous listener loop with automatic exponential backoff reconnection."""
        self._running = True
        log.info("binance_ws_collector_started")

        while self._running:
            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                log.info("binance_ws_cancelled")
                break
            except Exception as exc:
                self._consecutive_failures += 1
                wait_secs = min(2 ** self._consecutive_failures, 60)
                log.warning(
                    "binance_ws_disconnected",
                    error=str(exc),
                    retry_in_seconds=wait_secs,
                    consecutive_failures=self._consecutive_failures,
                )
                await asyncio.sleep(wait_secs)

    async def _connect_and_stream(self) -> None:
        from config.settings import settings  # local import: kline interval only

        symbols = await self.store.get_symbols()
        self._subscribed_version = self.store.symbols_version
        if not symbols:
            log.warning("binance_ws_no_symbols_configured")
            await asyncio.sleep(10)
            return

        if len(symbols) > self._LARGE_WATCHLIST_WARN:
            log.warning(
                "binance_ws_large_watchlist",
                count=len(symbols),
                hint="Render's free tier has limited CPU/RAM — consider trimming "
                     "the watchlist from the dashboard's Crypto tab",
            )

        # Build combined stream URLs: <symbol>@kline_<interval> and <symbol>@miniTicker
        interval = settings.crypto_kline_interval
        streams = []
        for sym in symbols:
            s = sym.lower()
            streams.append(f"{s}@kline_{interval}")
            streams.append(f"{s}@miniTicker")

        stream_param = "/".join(streams)
        url = f"{self.BASE_WS_URL}?streams={stream_param}"

        log.info("binance_ws_connecting", symbols_count=len(symbols), streams_count=len(streams))

        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=10,
            max_size=10_000_000,
        ) as ws:
            self._consecutive_failures = 0
            log.info("binance_ws_connected_successfully")

            watcher = asyncio.create_task(self._watch_for_resubscribe(ws))
            try:
                async for message in ws:
                    if not self._running:
                        break

                    self._total_messages_received += 1
                    self._last_message_time = datetime.now(timezone.utc)

                    try:
                        payload = json.loads(message)
                        await self._handle_stream_payload(payload)
                    except Exception as exc:
                        log.error("binance_ws_message_handling_failed", error=str(exc))
            finally:
                watcher.cancel()

    async def _watch_for_resubscribe(self, ws) -> None:
        """
        Force a reconnect when the watchlist changes (add/remove from the
        Crypto tab) so the new symbol set takes effect within ~15s instead of
        waiting for the next natural disconnect.
        """
        try:
            while True:
                await asyncio.sleep(15)
                if self.store.symbols_version != self._subscribed_version:
                    log.info("binance_ws_watchlist_changed_reconnecting")
                    await ws.close()
                    return
        except asyncio.CancelledError:
            pass

    async def _handle_stream_payload(self, payload: dict) -> None:
        stream_name = payload.get("stream", "")
        data = payload.get("data", {})

        if "@kline_" in stream_name:
            k = data.get("k", {})
            sym = k.get("s", "").lower()
            if not sym:
                return

            open_price = float(k.get("o", 0.0))
            high_price = float(k.get("h", 0.0))
            low_price = float(k.get("l", 0.0))
            close_price = float(k.get("c", 0.0))
            volume = float(k.get("v", 0.0))
            is_closed = bool(k.get("x", False))

            ts_ms = k.get("t", 0)
            timestamp = (
                datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
                if ts_ms > 0
                else datetime.now(timezone.utc)
            )

            await self.store.update_kline(
                symbol=sym,
                open_=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=volume,
                timestamp=timestamp,
                is_closed=is_closed,
            )

        elif "@miniTicker" in stream_name:
            sym = data.get("s", "").lower()
            if not sym:
                return

            close_p = float(data.get("c", 0.0))
            open_24h = float(data.get("o", 0.0))
            high_24h = float(data.get("h", 0.0))
            low_24h = float(data.get("l", 0.0))
            vol_24h = float(data.get("v", 0.0))

            await self.store.update_24h_stats(
                symbol=sym,
                price_24h_ago=open_24h,
                volume_24h=vol_24h,
                high_24h=high_24h,
                low_24h=low_24h,
            )

    def stop(self) -> None:
        self._running = False
