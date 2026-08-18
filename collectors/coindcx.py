from __future__ import annotations

from datetime import datetime, timezone

import httpx
import structlog

from analysis.crypto_state_store import CryptoStateStore

log = structlog.get_logger()

_API_BASE = "https://api.coindcx.com"


def _base_symbol(pair: str) -> str:
    """Strip the quote asset from a Binance-style pair, e.g. 'btcusdt' -> 'btc'."""
    p = pair.strip().lower()
    for quote in ("usdt", "busd", "usdc"):
        if p.endswith(quote) and len(p) > len(quote):
            return p[: -len(quote)]
    return p


class CoinDCXCollector:
    """
    REST-polling crypto price collector using CoinDCX's public ticker API.

    Free, no API key, no meaningful rate limit — one GET returns every market
    on the exchange in a single call. Preferred over CoinGecko for symbols
    it covers, since these are the exact prices you'd see trading on CoinDCX
    itself rather than an aggregate. CoinGecko fills in anything CoinDCX
    doesn't list.
    """

    TICKER_URL = f"{_API_BASE}/exchange/ticker"
    _QUOTES = ("USDT", "INR")  # try USDT pairs first, fall back to INR pairs

    def __init__(self, store: CryptoStateStore) -> None:
        self.store = store
        self._consecutive_failures = 0
        self.last_matched_symbols: set[str] = set()

    async def fetch(self) -> None:
        symbols = await self.store.get_symbols()
        if not symbols:
            return

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(self.TICKER_URL)

            if resp.status_code != 200:
                self._consecutive_failures += 1
                log.warning("coindcx_bad_status", status=resp.status_code, body=resp.text[:200])
                return

            rows = resp.json()
            by_market = {r.get("market", "").upper(): r for r in rows if r.get("market")}

            now = datetime.now(timezone.utc)
            matched: set[str] = set()
            for sym in symbols:
                base = _base_symbol(sym).upper()
                row = None
                for quote in self._QUOTES:
                    row = by_market.get(f"{base}{quote}")
                    if row:
                        break
                if not row:
                    continue

                try:
                    price = float(row.get("last_price") or 0.0)
                    if price <= 0:
                        continue
                    high = float(row.get("high") or price)
                    low = float(row.get("low") or price)
                    volume = float(row.get("volume") or 0.0)
                    change_pct = float(row.get("change_24_hour") or 0.0)
                except (TypeError, ValueError):
                    continue

                await self.store.update_from_rest(
                    symbol=sym,
                    price=price,
                    high_24h=high,
                    low_24h=low,
                    volume_24h=volume,
                    change_24h_pct=change_pct,
                    timestamp=now,
                )
                matched.add(sym)

            self.last_matched_symbols = matched
            self._consecutive_failures = 0
            log.info("coindcx_poll_done", matched=len(matched), requested=len(symbols))
        except Exception as exc:
            self._consecutive_failures += 1
            log.warning("coindcx_fetch_failed", error=str(exc))
