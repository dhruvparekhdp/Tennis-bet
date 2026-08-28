"""
Live order placement on Delta Exchange India. Real money.

Everything here is written on the assumption that a bug in this file costs
money rather than accuracy, so the defaults are the safe ones and the caller
has to opt in at every step.

Four rails, none of them removable by configuration alone:

  * OFF by default. `delta_trading_enabled` is False, and with no API key the
    client refuses regardless of the flag.
  * DRY RUN by default. With `delta_dry_run` set the client builds and logs the
    exact payload and sends nothing, so the whole path can be exercised against
    the real account with no order reaching the book.
  * A HARD notional cap, clamped in code rather than trusted from config. An
    order above it is refused, not silently resized — a resize hides the fact
    that the caller asked for something it should not have.
  * NO NAKED ENTRIES. An order without a stop is rejected before it is built.
    At 10x, an unprotected position is a way to lose the account to one wick.

Sizing is in CONTRACTS, not coin. Delta quotes perpetuals in contracts with a
fixed underlying value per contract — 0.001 BTC, 0.01 ETH — so a rupee amount
has to be converted through price, contract value and the USDT rate, and the
result is an integer. At a Rs300 margin this quantises hard: one ETH contract
is about Rs2,570 of notional, so 10x leverage buys exactly one contract and
nothing finer. Rounding that up would silently double the risk, so it rounds
down and refuses at zero.

Unverified against the live API — written from Delta's published REST shape
with no route to the venue from the build sandbox. Run it in dry-run first and
compare the logged payload against the venue's docs before switching it live.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from dataclasses import dataclass

import httpx
import structlog

from collectors.delta_exchange import BASE_URL, venue_symbol

log = structlog.get_logger()

# Delta rejects requests without one.
USER_AGENT = "tennis-bet/1.0"

# Price levels the venue may use to trigger a stop. Mark price is the default
# here: last-traded can be poked by a single thin print, and a stop that fires
# on someone else's bad fill is a loss with no cause.
STOP_TRIGGER_METHOD = "mark_price"


class OrderRefused(Exception):
    """A rail stopped this order. The message says which one."""


@dataclass(frozen=True)
class ContractSpec:
    """What one contract of a market is worth, read from the venue."""

    product_id: int
    symbol: str
    contract_value: float       # underlying per contract, e.g. 0.01 ETH
    tick_size: float

    def notional_usdt(self, price: float, contracts: int) -> float:
        return price * self.contract_value * contracts


def contracts_for(margin_inr: float, leverage: float, price_usdt: float,
                  spec: ContractSpec, usdt_inr: float) -> int:
    """
    How many whole contracts a rupee margin buys. Rounds DOWN, always.

    Rounding up is the tempting choice when the answer is 1.4 contracts and the
    difference looks small. It is not small: it is 40% more risk than the
    caller asked for, taken silently, on every trade.
    """
    if min(margin_inr, leverage, price_usdt, usdt_inr) <= 0:
        return 0
    if spec.contract_value <= 0:
        return 0
    notional_usdt = (margin_inr * leverage) / usdt_inr
    per_contract = price_usdt * spec.contract_value
    if per_contract <= 0:
        return 0
    return int(notional_usdt // per_contract)


class DeltaTradingClient:
    """
    Signed, rate-limited, capped access to the order endpoints.

    Construct it with everything it needs to refuse: a cap, a dry-run flag and
    an enabled flag. It never reads global settings itself, so a test cannot be
    made dangerous by an environment variable.
    """

    def __init__(self, api_key: str = "", api_secret: str = "", *,
                 enabled: bool = False, dry_run: bool = True,
                 max_notional_inr: float = 3000.0, usdt_inr: float = 102.0,
                 timeout: float = 15.0) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.enabled = enabled
        self.dry_run = dry_run
        self.max_notional_inr = max(0.0, max_notional_inr)
        self.usdt_inr = usdt_inr
        self.timeout = timeout
        self._specs: dict[str, ContractSpec] = {}
        self.last_payload: dict | None = None

    # ── auth ──────────────────────────────────────────────────────────────

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret)

    def _sign(self, method: str, path: str, query: str, body: str) -> tuple[str, str]:
        """
        signature = HMAC-SHA256(secret, method + timestamp + path + query + body)

        Copied from the venue's own client rather than from prose, because
        every part of it is load-bearing and none of it is guessable:

          * the timestamp is unix SECONDS, and it is both signed and sent as a
            header — generate it twice and the two disagree under load, and
            every request fails verification for a reason that looks random
          * the query string includes its leading "?" and is empty when there
            are no params, not "?"
          * the body is compact JSON with no spaces. `json.dumps` default
            separators insert ", " and produce a different string from the one
            the venue hashes, so the signature is correct for a body nobody sent
        """
        ts = str(int(time.time()))
        message = f"{method}{ts}{path}{query}{body}"
        signature = hmac.new(self.api_secret.encode(), message.encode(),
                             hashlib.sha256).hexdigest()
        return ts, signature

    @staticmethod
    def _query_string(params: dict | None) -> str:
        """
        Exactly the venue client's construction: insertion order, quote_plus.

        NOT sorted. Sorting looks tidier and produces a different string
        whenever two params are out of alphabetical order, which signs one
        request and sends another.
        """
        if not params:
            return ""
        return "?" + "&".join(f"{k}={urllib.parse.quote_plus(str(v))}"
                              for k, v in params.items())

    @staticmethod
    def _body_string(body: dict | None) -> str:
        return json.dumps(body, separators=(",", ":")) if body else ""

    async def _request(self, method: str, path: str, *, params: dict | None = None,
                       body: dict | None = None, signed: bool = True):
        query = self._query_string(params)
        payload = self._body_string(body)

        headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
        if signed:
            if not self.has_credentials:
                raise OrderRefused("no API credentials configured")
            ts, sig = self._sign(method, path, query, payload)
            headers.update({"api-key": self.api_key, "timestamp": ts, "signature": sig})

        url = f"{BASE_URL}{path}{query}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.request(method, url, headers=headers,
                                     content=payload or None)
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text[:500]}
        if r.status_code >= 400:
            log.warning("delta_api_error", path=path, status=r.status_code, body=data)
        return r.status_code, data

    @staticmethod
    def _result(data):
        """Delta wraps everything in {success, result, error}."""
        if isinstance(data, dict) and "result" in data:
            return data["result"]
        return data

    # ── contract specs ────────────────────────────────────────────────────

    async def load_specs(self) -> int:
        """
        Read product ids and contract values from the venue.

        Not hardcoded: a contract value is the thing that converts rupees into
        position size, and a stale one is wrong by whatever factor the venue
        changed it by. Cheap to fetch, catastrophic to guess.
        """
        status, data = await self._request("GET", "/v2/products", signed=False)
        if status != 200:
            return 0
        rows = data.get("result", data) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return 0
        wanted = {v: k for k, v in
                  __import__("collectors.delta_exchange", fromlist=["SYMBOL_MAP"])
                  .SYMBOL_MAP.items()}
        found = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            vs = str(row.get("symbol", ""))
            ours = wanted.get(vs)
            if not ours:
                continue
            try:
                self._specs[ours] = ContractSpec(
                    product_id=int(row["id"]), symbol=vs,
                    contract_value=float(row.get("contract_value") or 0.0),
                    tick_size=float(row.get("tick_size") or 0.0))
                found += 1
            except (KeyError, TypeError, ValueError):
                continue
        log.info("delta_specs_loaded", found=found)
        return found

    def spec_for(self, symbol: str) -> ContractSpec | None:
        return self._specs.get(symbol.lower())

    # ── the rails ─────────────────────────────────────────────────────────

    def check(self, symbol: str, price: float, contracts: int,
              stop_price: float | None) -> ContractSpec:
        """
        Every reason to refuse, checked before anything is built.

        Raises rather than returning a flag so a caller cannot forget to look.
        """
        if not self.enabled:
            raise OrderRefused("live trading is disabled")
        if not self.has_credentials:
            raise OrderRefused("no API credentials configured")
        if not venue_symbol(symbol):
            raise OrderRefused(f"{symbol} is not listed on this venue")
        spec = self.spec_for(symbol)
        if spec is None:
            raise OrderRefused(f"no contract spec loaded for {symbol}")
        if contracts < 1:
            raise OrderRefused(
                "margin buys less than one contract — increase margin or "
                "leverage rather than rounding up")
        if stop_price is None or stop_price <= 0:
            raise OrderRefused("refusing an entry with no stop attached")
        if price <= 0:
            raise OrderRefused("no price")

        notional_inr = spec.notional_usdt(price, contracts) * self.usdt_inr
        if notional_inr > self.max_notional_inr:
            raise OrderRefused(
                f"notional Rs{notional_inr:,.0f} exceeds the Rs"
                f"{self.max_notional_inr:,.0f} cap")
        return spec

    # ── orders ────────────────────────────────────────────────────────────

    def build_entry(self, symbol: str, is_long: bool, price: float,
                    contracts: int, stop_price: float) -> dict:
        """
        The entry order body. Pure, so a test can read it without a network.

        `stop_price` is not sent here — the venue attaches brackets through a
        separate endpoint — but it IS required, because `check` refuses an
        entry with no stop planned. Making it an argument is what stops a
        caller reaching this function without having decided where the trade
        is wrong.
        """
        spec = self.check(symbol, price, contracts, stop_price)
        return {
            "product_id": spec.product_id,
            "size": int(contracts),
            "side": "buy" if is_long else "sell",
            "order_type": "market_order",
            "time_in_force": "ioc",
            "reduce_only": "false",
            "post_only": "false",
        }

    def build_bracket(self, symbol: str, is_long: bool, stop_price: float,
                      target_price: float | None = None,
                      trail_amount: float | None = None) -> dict:
        """
        The bracket body for POST /v2/orders/bracket.

        A trail amount is signed by side in the venue's own client — positive
        to buy, negative to sell — so the sign is applied here rather than
        left to the caller to remember.
        """
        spec = self.spec_for(symbol)
        if spec is None:
            raise OrderRefused(f"no contract spec loaded for {symbol}")
        body: dict = {
            "product_id": spec.product_id,
            "product_symbol": spec.symbol,
            "bracket_stop_loss_price": _price(stop_price),
            "bracket_stop_loss_limit_price": _price(stop_price),
            "bracket_stop_trigger_method": STOP_TRIGGER_METHOD,
        }
        if target_price:
            body["bracket_take_profit_price"] = _price(target_price)
            body["bracket_take_profit_limit_price"] = _price(target_price)
        if trail_amount:
            amount = abs(trail_amount) if is_long else -abs(trail_amount)
            body["bracket_trail_amount"] = _price(amount)
        return body

    async def set_leverage(self, symbol: str, leverage: float) -> dict:
        """Order leverage is per product and sticky — set it before entering."""
        spec = self.spec_for(symbol)
        if spec is None:
            raise OrderRefused(f"no contract spec loaded for {symbol}")
        if self.dry_run:
            return {"dry_run": True, "leverage": leverage}
        status, data = await self._request(
            "POST", f"/v2/products/{spec.product_id}/orders/leverage",
            body={"leverage": str(leverage)})
        return {"status": status, "response": data}

    async def place(self, symbol: str, is_long: bool, price: float, contracts: int,
                    stop_price: float, target_price: float | None = None,
                    trail_amount: float | None = None) -> dict:
        """
        Enter, then protect. Two calls, because the venue has two endpoints.

        That gap is the dangerous part of this whole file: between the fill and
        the bracket the position is live with no stop. It cannot be closed by
        ordering the calls differently — a bracket has nothing to attach to
        before there is a position — so it is handled instead: if the bracket
        call fails, the position is closed immediately rather than left naked.
        A flat trade you did not want costs a round trip; an unprotected 10x
        position costs whatever the market does next.

        Dry run runs every check and builds both real bodies, sending neither.
        A rehearsal that skipped the rails would rehearse nothing.
        """
        entry = self.build_entry(symbol, is_long, price, contracts, stop_price)
        bracket = self.build_bracket(symbol, is_long, stop_price, target_price,
                                     trail_amount)
        self.last_payload = {"entry": entry, "bracket": bracket}
        spec = self.spec_for(symbol)
        notional_inr = spec.notional_usdt(price, contracts) * self.usdt_inr

        if self.dry_run:
            log.info("delta_dry_run", symbol=symbol, entry=entry, bracket=bracket,
                     notional_inr=round(notional_inr, 2))
            return {"dry_run": True, "entry": entry, "bracket": bracket,
                    "notional_inr": round(notional_inr, 2)}

        status, data = await self._request("POST", "/v2/orders", body=entry)
        if status >= 400:
            log.warning("delta_entry_rejected", symbol=symbol, status=status)
            return {"ok": False, "stage": "entry", "status": status,
                    "response": data, "entry": entry}

        b_status, b_data = await self._request("POST", "/v2/orders/bracket",
                                               body=bracket)
        if b_status >= 400:
            log.error("delta_bracket_failed_closing", symbol=symbol,
                      status=b_status, response=b_data)
            closed = await self.close(symbol)
            return {"ok": False, "stage": "bracket", "status": b_status,
                    "response": b_data, "position_closed": closed,
                    "entry": entry, "bracket": bracket}

        log.info("delta_order_live", symbol=symbol,
                 notional_inr=round(notional_inr, 2))
        return {"ok": True, "entry": entry, "bracket": bracket,
                "entry_response": self._result(data),
                "bracket_response": self._result(b_data),
                "notional_inr": round(notional_inr, 2)}

    async def close(self, symbol: str | None = None) -> dict:
        """
        Flatten. Used by the kill switch and by the naked-position recovery.

        Works with `enabled` False on purpose: the moment you most need to be
        flat is right after switching trading off, and a safety control that
        only works while the dangerous thing is on is not a control.
        """
        if not self.has_credentials:
            raise OrderRefused("no API credentials configured")
        body: dict = {"close_all_isolated": True, "close_all_cross": True}
        if symbol:
            spec = self.spec_for(symbol)
            if spec is not None:
                body = {"close_all_isolated": True, "close_all_cross": True,
                        "product_ids": [spec.product_id]}
        if self.dry_run:
            return {"dry_run": True, "would_close": body}
        status, data = await self._request("POST", "/v2/positions/close_all",
                                           body=body)
        log.warning("delta_close_all", status=status, body=body)
        return {"status": status, "response": data}

    async def balances(self) -> list[dict]:
        status, data = await self._request("GET", "/v2/wallet/balances")
        rows = self._result(data) if status == 200 else []
        return rows if isinstance(rows, list) else []

    async def live_orders(self) -> list[dict]:
        status, data = await self._request("GET", "/v2/orders")
        rows = self._result(data) if status == 200 else []
        return rows if isinstance(rows, list) else []

    async def positions(self) -> list[dict]:
        status, data = await self._request("GET", "/v2/positions/margined")
        rows = self._result(data) if status == 200 else []
        return rows if isinstance(rows, list) else []

    async def cancel_all(self, symbol: str | None = None) -> dict:
        """
        Pull every resting order. There is no DELETE /v2/orders/all on this
        venue — cancelling is per product, so the live orders are read first
        and cancelled in a batch per product.
        """
        if not self.has_credentials:
            raise OrderRefused("no API credentials configured")
        orders = await self.live_orders()
        if symbol:
            spec = self.spec_for(symbol)
            if spec is not None:
                orders = [o for o in orders
                          if o.get("product_id") == spec.product_id]
        if not orders:
            return {"cancelled": 0}
        if self.dry_run:
            return {"dry_run": True, "would_cancel": len(orders)}

        by_product: dict[int, list[dict]] = {}
        for o in orders:
            pid = o.get("product_id")
            if pid is None or o.get("id") is None:
                continue
            by_product.setdefault(int(pid), []).append(
                {"id": o["id"], "product_id": int(pid)})
        cancelled = 0
        for pid, batch in by_product.items():
            status, _ = await self._request(
                "DELETE", "/v2/orders/batch",
                body={"product_id": pid, "orders": batch})
            if status < 400:
                cancelled += len(batch)
        log.warning("delta_cancel_all", cancelled=cancelled)
        return {"cancelled": cancelled}


def _price(value: float) -> str:
    """Prices go over the wire as strings, trimmed of trailing zeros."""
    return f"{value:.8f}".rstrip("0").rstrip(".")
