from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone

import structlog
import websockets

from analysis.crypto_state_store import CryptoStateStore

log = structlog.get_logger()


# Binance serves market data from several hosts. The main one geo-blocks a lot
# of cloud IPs (returns HTTP 451 during the upgrade handshake), but the public
# market-data mirrors often are not blocked, so we try them in order.
BINANCE_WS_HOSTS = [
    "wss://data-stream.binance.vision/stream",   # public market-data mirror, usually not geo-blocked
    "wss://stream.binance.com:9443/stream",      # main host — blocked from US cloud IPs
    "wss://stream.binance.com:443/stream",       # same host, 443 (some networks only allow 443)
]


def is_geoblocked(exc: Exception) -> bool:
    """True if this looks like Binance's HTTP 451 geo-block rather than a transient drop.

    451 is returned during the HTTP upgrade, before the WebSocket ever opens, and it
    depends only on the server's IP location — retrying the same host can never fix it,
    so we fail over to the next host instead of looping.
    """
    text = str(exc)
    return "451" in text or "Unavailable For Legal Reasons" in text


class BinanceWSCollector:
    """
    Real-time Binance WebSocket collector streaming Klines for every symbol in
    the DB-backed watchlist.

    Binance klines carry true OHLC (real high/low per candle), which the REST
    pollers cannot provide — CoinDCX/CoinGecko snapshots collapse to
    open==high==low==close, which makes ATR (and therefore every signal's
    target/stop distance) far too small. So when Binance is reachable it
    materially improves signal quality, not just latency.

    Reachability is the catch: the main host returns HTTP 451 from most US
    cloud IPs including Render's. We try the public market-data mirrors first
    and treat 451 as "this host is unusable here" rather than retrying it
    forever (which is what burned CPU in the earlier deploy).
    """

    _LARGE_WATCHLIST_WARN = 25  # Render free tier has limited CPU/RAM — flag heavy watchlists
    _MAX_ROUNDS_ALL_BLOCKED = 2  # give up after every host 451s this many times

    def __init__(self, store: CryptoStateStore) -> None:
        self.store = store
        self._running = False
        self._consecutive_failures = 0
        self._total_messages_received = 0
        self._last_message_time: datetime | None = None
        self._subscribed_version = -1
        self._kline_interval = "1m"
        self._hosts = list(BINANCE_WS_HOSTS)
        self._host_idx = 0
        self._blocked_hosts: set[str] = set()
        self._geoblocked_rounds = 0
        self.active_host: str | None = None
        self.last_error: str | None = None

    @property
    def BASE_WS_URL(self) -> str:  # noqa: N802 — kept for backwards compatibility
        return self._hosts[self._host_idx % len(self._hosts)]

    def _advance_host(self) -> None:
        self._host_idx = (self._host_idx + 1) % len(self._hosts)

    async def run_forever(self) -> None:
        """Continuous listener loop with host failover and exponential backoff."""
        self._running = True
        log.info("binance_ws_collector_started", hosts=self._hosts)

        while self._running:
            # Every candidate host geo-blocked us repeatedly — stop rather than
            # spin forever. CoinDCX/CoinGecko REST polling keeps prices flowing.
            if len(self._blocked_hosts) >= len(self._hosts):
                self._geoblocked_rounds += 1
                if self._geoblocked_rounds >= self._MAX_ROUNDS_ALL_BLOCKED:
                    self.last_error = (
                        "All Binance hosts returned HTTP 451 (geo-blocked from this "
                        "server's IP). Stopping — CoinDCX/CoinGecko continue to supply prices."
                    )
                    log.warning("binance_ws_all_hosts_geoblocked_giving_up",
                                hosts=sorted(self._blocked_hosts))
                    self._running = False
                    return
                self._blocked_hosts.clear()  # one more full sweep before giving up

            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                log.info("binance_ws_cancelled")
                break
            except Exception as exc:
                self.last_error = str(exc)
                host = self.BASE_WS_URL
                if is_geoblocked(exc):
                    # Retrying this host is pointless — it depends on our IP, not the network.
                    self._blocked_hosts.add(host)
                    self.active_host = None
                    self._advance_host()
                    log.warning("binance_ws_geoblocked", host=host,
                                next_host=self.BASE_WS_URL, error=str(exc)[:120])
                    await asyncio.sleep(1)
                    continue

                self._consecutive_failures += 1
                wait_secs = min(2 ** self._consecutive_failures, 60)
                # A host that keeps failing for non-451 reasons is also worth rotating away from.
                if self._consecutive_failures % 3 == 0:
                    self._advance_host()
                log.warning(
                    "binance_ws_disconnected",
                    host=host,
                    error=str(exc)[:160],
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
        host = self.BASE_WS_URL
        url = f"{host}?streams={stream_param}"

        # Optional last-resort escape hatch: route through a proxy that exits in a
        # region Binance doesn't block. Only pass the kwarg when it's actually set —
        # websockets' `proxy` defaults to True (use system proxy config), and passing
        # None would explicitly DISABLE proxying rather than fall back to the default.
        kwargs: dict = {}
        proxy_url = os.getenv("BINANCE_PROXY_URL") or os.getenv("PROXY_URL")
        if proxy_url:
            kwargs["proxy"] = proxy_url

        log.info("binance_ws_connecting", host=host,
                 symbols_count=len(symbols), streams_count=len(streams),
                 via_proxy=bool(proxy_url))

        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=10,
            max_size=10_000_000,
            **kwargs,
        ) as ws:
            self._consecutive_failures = 0
            self._geoblocked_rounds = 0
            self.active_host = host
            self.last_error = None
            log.info("binance_ws_connected_successfully", host=host)

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
