from __future__ import annotations

from datetime import datetime, timezone

import httpx
import structlog

from analysis.crypto_state_store import CryptoStateStore

log = structlog.get_logger()

_API_BASE = "https://api.coindcx.com"
_PUBLIC_BASE = "https://public.coindcx.com"

# /exchange/ticker covers SPOT markets only. Instruments that exist only as
# futures — gold and silver among them — are absent from it, which is why
# XAUUSDT sat at $0.000000 on the dashboard while trading fine in the app.
_FUTURES_PRICE_URLS = (
    f"{_PUBLIC_BASE}/market_data/v3/current_prices/futures/rt",
    f"{_PUBLIC_BASE}/market_data/v3/current_prices/futures",
)


def _futures_name_variants(base: str) -> tuple[str, ...]:
    """
    Plausible spellings of one instrument on the futures venue.

    CoinDCX prefixes futures instruments (B-XAU_USDT and similar) and the exact
    family letter is not documented publicly. Rather than guess one and have
    the symbol silently stay at zero, every reasonable spelling is tried and
    the one that matches is logged.
    """
    b = base.upper()
    return (
        f"B-{b}_USDT", f"F-{b}_USDT", f"{b}_USDT", f"{b}USDT",
        f"B-{b}_INR", f"{b}_INR", f"{b}INR",
    )


def _extract_price(node) -> float | None:
    """Pull a last price out of whichever shape the endpoint returns."""
    if isinstance(node, (int, float)):
        return float(node) or None
    if isinstance(node, dict):
        for key in ("ls", "last_price", "mark_price", "price", "c", "close"):
            v = node.get(key)
            if v is None:
                continue
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f > 0:
                return f
    return None


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

            # Anything the spot ticker did not cover may still be a futures
            # instrument. Gold is the case that prompted this.
            missing = [s for s in symbols if s not in matched]
            if missing:
                matched |= await self._fetch_futures(missing, now)

            self.last_matched_symbols = matched
            unmatched = sorted(set(symbols) - matched)
            if unmatched:
                log.warning("coindcx_symbols_unmatched", symbols=unmatched)
            self._consecutive_failures = 0
            log.info("coindcx_poll_done", matched=len(matched), requested=len(symbols))
        except Exception as exc:
            self._consecutive_failures += 1
            log.warning("coindcx_fetch_failed", error=str(exc))

    async def fetch_futures_raw(self) -> tuple[str, object] | None:
        """
        Return (url, payload) from the first futures endpoint that answers.

        Exists so the shape can be inspected from the running server — the
        futures response format is not publicly documented and could not be
        reached from the environment this was written in.
        """
        async with httpx.AsyncClient(timeout=15.0) as client:
            for url in _FUTURES_PRICE_URLS:
                try:
                    r = await client.get(url)
                except Exception as exc:
                    log.debug("coindcx_futures_unreachable", url=url, error=str(exc))
                    continue
                if r.status_code == 200:
                    try:
                        return url, r.json()
                    except Exception:
                        return url, r.text[:2000]
        return None

    async def _fetch_futures(self, symbols: list[str], now: datetime) -> set[str]:
        """Fill in symbols the spot ticker does not list. Best effort, never fatal."""
        matched: set[str] = set()
        got = await self.fetch_futures_raw()
        if not got:
            return matched
        url, payload = got

        # The payload has been seen as a bare mapping and as one wrapped in a
        # "prices" key; accept either rather than depending on which.
        table = payload
        if isinstance(payload, dict):
            for wrapper in ("prices", "data", "result"):
                inner = payload.get(wrapper)
                if isinstance(inner, dict):
                    table = inner
                    break
        if not isinstance(table, dict):
            log.warning("coindcx_futures_unexpected_shape",
                        url=url, type=type(table).__name__)
            return matched

        upper = {str(k).upper(): v for k, v in table.items()}
        for sym in symbols:
            base = _base_symbol(sym).upper()
            for name in _futures_name_variants(base):
                price = _extract_price(upper.get(name))
                if price is None:
                    continue
                await self.store.update_from_rest(
                    symbol=sym, price=price, high_24h=price, low_24h=price,
                    volume_24h=0.0, change_24h_pct=0.0, timestamp=now,
                )
                matched.add(sym)
                log.info("coindcx_futures_matched", symbol=sym,
                         instrument=name, price=price)
                break
        return matched
