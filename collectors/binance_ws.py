from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import structlog
import websockets

from analysis.crypto_state_store import CryptoStateStore
from config.settings import settings

log = structlog.get_logger()


class BinanceWSCollector:
    """
    Real-time Binance WebSocket collector streaming Klines & 24h stats
    for all symbols in the configurable Top 50 watchlist.
    """

    BASE_WS_URL = "wss://stream.binance.com:9443/stream"

    def __init__(self, store: CryptoStateStore) -> None:
        self.store = store
        self._running = False
        self._consecutive_failures = 0
        self._total_messages_received = 0
        self._last_message_time: datetime | None = None

    async def run_forever(self) -> None:
        """Continuous listener loop with automatic exponential backoff reconnection."""
        self._running = True
        log.info("binance_ws_collector_started", symbols_count=len(settings.crypto_symbols))

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
        symbols = settings.crypto_symbols
        if not symbols:
            log.warning("binance_ws_no_symbols_configured")
            await asyncio.sleep(10)
            return

        # Build combined stream URLs: <symbol>@kline_<interval> and <symbol>@miniTicker
        interval = settings.crypto_kline_interval
        streams = []
        for sym in symbols:
            s = sym.lower()
            streams.append(f"{s}@kline_{interval}")
            streams.append(f"{s}@miniTicker")

        stream_param = "/".join(streams)
        url = f"{self.BASE_WS_URL}?streams={stream_param}"

        log.info("binance_ws_connecting", streams_count=len(streams))

        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=10,
            max_size=10_000_000,
        ) as ws:
            self._consecutive_failures = 0
            log.info("binance_ws_connected_successfully")

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
