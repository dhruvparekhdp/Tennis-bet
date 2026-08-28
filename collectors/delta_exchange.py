"""
Delta Exchange India — market data, depth, and (separately) live orders.

Why this venue was added
------------------------
Two things this system could not get anywhere else. Real per-bar volume: the
CoinDCX ticker publishes a rolling 24-hour figure, so every volume measure read
a constant until candles were pulled separately. And a public L2 order book,
which is the only input here that is not a transformation of past prices — it
is what fixes the guessed spread and slippage in the cost floor.

Read and write are deliberately different classes. Everything in this file is
public, keyless and harmless. Placing orders lives in `DeltaTradingClient`,
which is separately gated, separately configured and refuses to do anything
unless it has been switched on on purpose.

Unverified against the live API. This was written from Delta's published REST
shape while the sandbox had no route to the venue, so every parser tolerates
the field names moving and every failure is non-fatal — the collector degrades
to "no data" rather than taking the price feed down with it. `/api/debug/delta`
prints the raw response so a changed shape can be recognised rather than
guessed at.
"""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import structlog

log = structlog.get_logger()

BASE_URL = "https://api.india.delta.exchange"

# Delta names perpetuals BTCUSD / ETHUSD, not BTCUSDT. Mapping rather than
# string surgery, because the exceptions are the point: a lookup that silently
# invents "XAUUSD" for a market the venue does not list produces 404s that look
# like an outage.
SYMBOL_MAP = {
    "btcusdt": "BTCUSD",
    "ethusdt": "ETHUSD",
    "solusdt": "SOLUSD",
    "xrpusdt": "XRPUSD",
    "ltcusdt": "LTCUSD",
    "bchusdt": "BCHUSD",
    "adausdt": "ADAUSD",
    "dogeusdt": "DOGEUSD",
    "avaxusdt": "AVAXUSD",
    "linkusdt": "LINKUSD",
    # Gold and silver, which the venue lists as tokenised perpetuals at a
    # tenth of the crypto fee — the cheap-to-trade markets the cost model has
    # always had a separate frame for.
    "xauusdt": "XAUTUSD",
    "paxgusdt": "PAXGUSD",
    "xagusdt": "SLVONUSD",
}


def venue_symbol(symbol: str) -> str | None:
    """Our symbol to Delta's, or None when the venue does not list it."""
    return SYMBOL_MAP.get(symbol.lower().strip())


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_candles(payload) -> list[dict]:
    """
    Normalise Delta's candle response into plain dicts, oldest first.

    Written tolerantly: the result may arrive under `result`, as a bare list,
    with the timestamp as `time` or `t`, in seconds or milliseconds. A parser
    that insists on one shape turns a working feed into a silent outage.
    """
    rows = payload
    if isinstance(payload, dict):
        rows = payload.get("result", payload.get("data", []))
    if not isinstance(rows, list):
        return []

    out: list[dict] = []
    for row in rows:
        try:
            if isinstance(row, dict):
                t = row.get("time", row.get("t", row.get("timestamp")))
                o, h = row.get("open", row.get("o")), row.get("high", row.get("h"))
                low, c = row.get("low", row.get("l")), row.get("close", row.get("c"))
                v = row.get("volume", row.get("v", 0.0))
            elif isinstance(row, (list, tuple)) and len(row) >= 6:
                t, o, h, low, c, v = row[:6]
            else:
                continue
            if t is None or o is None or c is None:
                continue
            ts = float(t)
            # Seconds or milliseconds. Guessing wrong puts every bar in 1970.
            if ts > 1e11:
                ts /= 1000.0
            out.append({
                "timestamp": datetime.fromtimestamp(ts, tz=UTC),
                "open": _num(o), "high": _num(h), "low": _num(low),
                "close": _num(c), "volume": max(0.0, _num(v)),
            })
        except (TypeError, ValueError, OSError, OverflowError):
            continue
    out.sort(key=lambda r: r["timestamp"])
    return out


def parse_orderbook(symbol: str, payload):
    """
    Normalise an L2 response into an OrderBook, or None.

    Bids must descend and asks must ascend — every downstream walk depends on
    it, and a venue that returns them the other way round would produce fill
    prices that flatter every trade rather than failing loudly.
    """
    from analysis.orderbook import OrderBook

    node = payload
    if isinstance(payload, dict):
        node = payload.get("result", payload.get("data", payload))
    if not isinstance(node, dict):
        return None

    def side(key_a: str, key_b: str) -> list[tuple[float, float]]:
        raw = node.get(key_a, node.get(key_b, []))
        levels: list[tuple[float, float]] = []
        if not isinstance(raw, list):
            return levels
        for row in raw:
            if isinstance(row, dict):
                price = _num(row.get("price", row.get("p")))
                size = _num(row.get("size", row.get("s", row.get("qty"))))
            elif isinstance(row, (list, tuple)) and len(row) >= 2:
                price, size = _num(row[0]), _num(row[1])
            else:
                continue
            if price > 0 and size > 0:
                levels.append((price, size))
        return levels

    bids = sorted(side("buy", "bids"), key=lambda x: -x[0])
    asks = sorted(side("sell", "asks"), key=lambda x: x[0])
    if not bids or not asks:
        return None
    return OrderBook(symbol=symbol, bids=bids, asks=asks)


class DeltaMarketData:
    """
    Public endpoints only. No key, no signature, nothing that can move money.

    Kept separate from the trading client on purpose: this one can be enabled
    freely and run on every deploy, and nothing it does is reversible-with-
    consequences.
    """

    def __init__(self, store=None, timeout: float = 15.0) -> None:
        self.store = store
        self.timeout = timeout
        self._consecutive_failures = 0
        self.last_books: dict[str, object] = {}

    async def _get(self, path: str, params: dict | None = None):
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(f"{BASE_URL}{path}", params=params or {})
        if r.status_code != 200:
            log.debug("delta_bad_status", path=path, status=r.status_code,
                      body=r.text[:200])
            return None
        try:
            return r.json()
        except Exception:
            return None

    async def fetch_candles_raw(self, symbol: str, resolution: str = "1m",
                                limit: int = 120):
        """(venue_symbol, payload) for one market, or None."""
        vs = venue_symbol(symbol)
        if not vs:
            return None
        now = int(datetime.now(UTC).timestamp())
        # Delta's history endpoint is start/end in seconds, not a bar count.
        minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}.get(resolution, 1)
        payload = await self._get("/v2/history/candles", {
            "resolution": resolution, "symbol": vs,
            "start": now - limit * minutes * 60, "end": now,
        })
        return (vs, payload) if payload is not None else None

    async def fetch_orderbook(self, symbol: str):
        """One L2 snapshot, parsed. None on any failure — never raises."""
        vs = venue_symbol(symbol)
        if not vs:
            return None
        try:
            payload = await self._get(f"/v2/l2orderbook/{vs}")
        except Exception as exc:
            log.debug("delta_book_unreachable", symbol=symbol, error=str(exc))
            return None
        if payload is None:
            return None
        book = parse_orderbook(symbol.lower(), payload)
        if book is not None:
            self.last_books[symbol.lower()] = book
        return book

    async def fetch(self) -> int:
        """
        Refresh candles and books for the watchlist. Returns markets refreshed.

        Best effort per symbol: one market failing must not stop the rest, and
        the whole job failing must not stop the price feed, which comes from a
        different venue entirely.
        """
        if self.store is None:
            return 0
        symbols = await self.store.get_symbols()
        refreshed = 0
        for sym in symbols:
            try:
                got = await self.fetch_candles_raw(sym)
                if got:
                    bars = parse_candles(got[1])
                    if bars:
                        await self.store.replace_candles(sym, bars)
                        refreshed += 1
                await self.fetch_orderbook(sym)
            except Exception as exc:
                log.debug("delta_symbol_failed", symbol=sym, error=str(exc))
        self._consecutive_failures = 0 if refreshed else self._consecutive_failures + 1
        log.info("delta_fetch_done", refreshed=refreshed, requested=len(symbols),
                 books=len(self.last_books))
        return refreshed
