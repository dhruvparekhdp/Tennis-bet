from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import structlog
import websockets

from analysis.crypto_state_store import CommodityStateStore
from config.settings import settings

log = structlog.get_logger()


class TwelveDataWSCollector:
    """
    Real-time Twelve Data WebSocket collector for Commodities
    (Gold XAU/USD, Silver XAG/USD, Crude Oil WTI/USD).
    """

    BASE_WS_URL = "wss://ws.twelvedata.com/v1/quotes/price"

    def __init__(self, store: CommodityStateStore) -> None:
        self.store = store
        self._running = False
        self._consecutive_failures = 0

    async def run_forever(self) -> None:
        if not settings.twelvedata_api_key:
            log.info("twelvedata_ws_skipped_no_api_key")
            return

        self._running = True
        log.info("twelvedata_ws_collector_started")

        while self._running:
            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                log.info("twelvedata_ws_cancelled")
                break
            except Exception as exc:
                self._consecutive_failures += 1
                wait_secs = min(2 ** self._consecutive_failures, 60)
                log.warning(
                    "twelvedata_ws_disconnected",
                    error=str(exc),
                    retry_in_seconds=wait_secs,
                )
                await asyncio.sleep(wait_secs)

    async def _connect_and_stream(self) -> None:
        url = f"{self.BASE_WS_URL}?apikey={settings.twelvedata_api_key}"
        symbols = [s.strip() for s in settings.twelvedata_symbols.split(",") if s.strip()]

        async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
            self._consecutive_failures = 0
            log.info("twelvedata_ws_connected")

            # Subscribe to symbols
            subscribe_payload = {
                "action": "subscribe",
                "params": {"symbols": ",".join(symbols)},
            }
            await ws.send(json.dumps(subscribe_payload))

            async for message in ws:
                if not self._running:
                    break

                try:
                    data = json.loads(message)
                    event = data.get("event")
                    if event == "price":
                        sym = data.get("symbol", "")
                        price = float(data.get("price", 0.0))
                        ts = data.get("timestamp", 0)
                        timestamp = (
                            datetime.fromtimestamp(ts, tz=timezone.utc)
                            if ts > 0
                            else datetime.now(timezone.utc)
                        )
                        await self.store.update_price(sym, price, timestamp)
                    elif event == "subscribe-status" and data.get("status") != "ok":
                        log.warning("twelvedata_subscribe_warning", data=data)
                except Exception as exc:
                    log.error("twelvedata_message_error", error=str(exc))

    def stop(self) -> None:
        self._running = False
