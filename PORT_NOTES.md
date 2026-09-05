# PORT_NOTES.md — Phase 0 inventory

Read of the source project (`Tennis-bet`, Python/aiohttp on Render) ahead of the SwiftUI
port into `dhruv`. No Swift written. Written from the code as deployed at commit `c2cbdbf`.

**Headline for the plan owner:** the known open question resolves as **no paid program
needed**, but only if the notification payload changes shape. Details in §9 — read that
before Phase 4. One other finding contradicts an assumption in the plan; flagged in §10.

---

## 1. What it monitors, and via which endpoints

Eight USDT perpetual markets, seeded from `crypto_watchlist_seed` and editable at runtime
from a DB table (`crypto_watchlist`) rather than a config file. Currently BTC, ETH, BNB,
SOL, XRP, LTC, BCH and XAU (gold).

| Purpose | Endpoint | Auth | Notes |
|---|---|---|---|
| **1-minute OHLCV (primary)** | `data-api.binance.vision/api/v3/klines` | none | Public mirror, tried first — the main host 451s from US cloud IPs |
| 1-minute OHLCV (fallbacks) | `api.binance.com`, `api1–3.binance.com` | none | Walked in order; the first 200 answers is remembered |
| Order book depth | `…/api/v3/depth` | none | 50 levels, same host list |
| OHLCV (last resort) | `public.coindcx.com/market_data/candles` | none | Worse data, but real bars |
| Spot ticker | `api.coindcx.com/exchange/ticker` | none | Price/24h stats between kline polls |
| Ticker fallback | `api.coingecko.com/api/v3` | optional key | Covers anything CoinDCX doesn't list |
| News sentiment | `cryptopanic.com/api/v1/posts` | **key required** | Optional feature; disabled without it |
| Fear & Greed | `api.alternative.me/fng/` | none | Adjusts confidence, never fires a trade |

**For the port:** only the first four rows matter. Binance klines + depth is the whole
market-data surface. Everything else is redundancy for a server that must not go dark.

## 2. Auth model

Effectively **none for the data that matters**. Every price, candle and depth endpoint the
signal path depends on is public and keyless. CoinGecko and CryptoPanic keys are optional
and only widen coverage.

The server holds a Telegram bot token, and previously held exchange trade keys — that path
has been removed entirely.

**Consequence for Phase 1:** `KeychainStore` has almost nothing to store on day one. Do not
let that make it optional; it is where the CryptoPanic key goes if news sentiment is ported,
and where any future exchange key must go. But the app can ship a full working watchlist and
alert engine with an empty Keychain, which makes Phase 1 simpler than the plan assumes.

## 3. Polling cadence, and what is actually essential

| Job | Interval | Essential? |
|---|---|---|
| `binance_klines` — candles + depth | **60 s** | **Yes.** Everything derives from it |
| `coindcx_poll` — ticker | 30 s | No. Keeps price fresh between kline polls |
| `coingecko_poll` | 60 s | No. Redundancy |
| `crypto_analysis` — run analyzers | **60 s** | **Yes** |
| `crypto_snapshot` — training rows | 120 s | No. Feeds an ML path not worth porting |
| `sentiment_refresh` / `crypto_news` | 30 s / 300 s | No |
| `paper_trading_tick` | 30 s | No. Server-side simulator |
| `self_ping` | 300 s | No. Render free-tier keep-alive; meaningless on iOS |

**Two jobs matter: fetch candles, then evaluate.** Both at 60 s. Everything else is either
redundancy for an always-on server or a feature the port can drop.

Note the cadence is **60 s, not sub-second**. This is the single most useful fact for
Phase 4 and it is why §9 lands where it does.

## 4. Every signal rule

Five analyzers run per symbol per cycle. All five route through one shared level policy
(`scalp_levels`) that decides *how far* — an analyzer only ever says *whether* and *which
way*. Preserve that separation; it is the reason the levels are consistent.

### 4.1 The five rules

| Rule | Fires when | Confidence | State |
|---|---|---|---|
| **Confluence** *(primary)* | ≥3 of 5 independent families agree | **Earned**: `0.55 + share×0.45 − dissent×0.30`, capped 0.92 | Stateless |
| **RSI Divergence** | Price makes a lower low while RSI makes a higher low (or mirror), measured at *confirmed swing pivots* over 40 bars | `min(0.85, 0.65 + distance_from_35_or_65 × 0.01)` | Stateless |
| **Bollinger Squeeze** | Was squeezed (BB inside Keltner), no longer is, and a range breakout confirms direction | **Fixed 0.70** | Needs previous bar's squeeze state |
| **Volume Surge** | `relative_volume ≥ 2.8` **and** swing structure does not point the other way | **Fixed 0.68** | Stateless |
| **Sentiment Shift** | `|sentiment| ≥ 0.35` and RSI has not already moved with it | `min(0.82, 0.62 + |score| × 0.20)` | Needs external news feed |

The five confluence families and weights: **trend 1.0, structure 1.0, momentum 0.8,
volume 0.7, pattern 0.5** — one vote each, so RSI/Stochastic/MACD (three views of momentum)
cannot be counted as three confirmations. Two vetoes sit outside the vote: volatility in the
bottom 20% of its own range, and relative volume below 0.60.

**Calibration warning for the port:** the two fixed confidences are not measurements. Live
data shows 70%-stated resolving at 47.6% (n=84) and 65%-stated at 35% (n=40). The *ranking*
is monotone and real; the *level* is 17–30 points optimistic. Do not let a Swift sizing
ladder key off these numbers without recalibrating them first.

### 4.2 The level policy — port this exactly

Given price, 1-bar ATR% and a per-market cost frame:

```
stop_pct   = max(atr_pct × 2.0,  cost_floor / 0.35)     # noise, and fee ≤ 35% of risk
target_pct = max(stop_pct × 2.0, cost_floor × 3.0)      # reward:risk 2, clears costs
minutes    = (target_pct / atr_pct)² × bar_minutes      # inverse sqrt-of-time
```

Cost floor is **per market**: crypto 0.168%, gold 0.0736%. Every number above was arrived at
by fixing a specific live failure; the reasoning is in the docstrings and is worth reading
before changing any of them.

### 4.3 Statefulness — the part that is easy to get wrong

Rules are nearly stateless. **The engine around them is not**, and it holds three pieces of
state that a naive port would drop:

1. **Cooldown** — `(symbol, signal_type) → last fired`, 15 min.
2. **Live call** — `(symbol, direction) → signal`. Suppresses a repeat while the previous
   one is unresolved. Cleared when price reaches target or stop, or after a TTL of
   `1.5 × timeframe` (min 30 min).
3. **Contradiction guard** — refuses a signal opposing a live one. Added after the live feed
   produced a confluence SHORT on ETH at 11:17 and a volume-spike LONG at 11:24. Both older
   guards were blind to it: one is keyed by direction, the other by signal type, so neither
   could see a conflict.

All three must exist in Swift or the app will emit the same contradictory pairs.

## 5. Persistence schema

Nine tables; the port needs three.

**Port these:**
- `crypto_signal_log` — every fired signal plus `outcome` (pending/won/lost/expired) and
  `pnl_pct`. This is the accuracy record. **The most valuable table.**
- `crypto_watchlist` — `symbol`, `added_at`.
- `crypto_snapshots` — price + indicators, with `price_30m_later`/`1h`/`4h`/`1d` filled in
  afterwards. Useful only if you want to score forecasts later; skip for v1.

**Do not port:** `paper_cycles`, `paper_positions`, `paper_trades` (server-side simulator),
`commodity_snapshots`, `news_sentiment`, and all tennis/football tables.

Candles are **never persisted** — they live in memory, capped at 360 bars per symbol, and
are refetched on boot. Copy this. It maps cleanly onto the plan's "capped history retention
with pruning on write", and it means a cold start costs one HTTP call per symbol.

## 6. How it notifies

`TelegramNotifier.send_signal()` → HTML message. One message per signal, plus paper-trade
closes and cycle summaries. No batching, no digest, no priority tiers.

Message content maps 1:1 onto a `UNNotificationContent`: symbol, direction, setup name,
expected time, the three levels, move as a multiple of cost, reward:risk, the trailing plan,
confidence, suggested stake. **See §9 before copying this payload verbatim.**

## 7. Tennis-era dead code

| Group | Files | Lines | Port? |
|---|---|---|---|
| Tennis analysis | 16 | 2,099 | **No** |
| Football analysis | 3 | 529 | **No** |
| Sports collectors | 15 | — | **No** |
| Paper trading | 3 | 2,052 | **No** — server-side simulator |
| **Crypto analysis** | **12** | **4,105** | **Yes** |

Sports code is fully isolated behind its own engines and routes — nothing in the crypto path
imports it. One trap: `analysis/scalping.py` is a **tennis** module. The crypto one is
`analysis/scalp_levels.py`. They were confused once already, with consequences.

The web layer (`scheduler/health.py`, 5,163 lines of embedded HTML/CSS/JS) is entirely
replaced by SwiftUI. Read it for behaviour, port none of it.

## 8. Computationally heavy work

Almost nothing, on the scale a phone cares about.

- **Indicators** (561 lines) — all O(n) or O(n·period) over ≤360 floats. Sub-millisecond.
- **Confluence** — five family votes over the same window. Trivial.
- **Patterns** — seven detectors over the last few bars. Trivial.
- **Backtest** — walks thousands of candles. Server-only; do not port.
- **`ml_predictor` / `multi_horizon_predictor`** — scikit-learn, numpy, pandas. **Do not
  port.** They are a training path feeding `crypto_snapshots`, not part of the signal flow.
  Excluding them removes the only reason to want a numerical stack on device.

**The entire signal path is pure arithmetic over ≤360 candles per symbol.** Eight symbols is
~3k floats. This runs comfortably inside a `BGAppRefreshTask` budget.

## 9. The known open question — resolved

**The plan asks: does this need a server pushing via APNs, i.e. the $99 program?**

**Answer: no — provided the notification stops carrying a fixed entry price.**

The reasoning is arithmetic, not preference. The system's own divergence guard refuses to
act when the signal price and the live price differ by more than 0.15%, because past that
the published levels stop describing the market. So the useful life of a published entry is
however long that market takes to drift 0.15%:

| | 1-min ATR | entry goes stale in | moves in 15 min | vs the 0.48% stop |
|---|---|---|---|---|
| BNB | 0.056% | 7.2 min | 0.217% | 45% |
| BTC | 0.059% | 6.5 min | 0.229% | 48% |
| ETH | 0.081% | 3.4 min | 0.314% | 65% |
| SOL | 0.109% | 1.9 min | 0.422% | 88% |

`BGAppRefreshTask` fires at ~15 minutes **at best**. Replaying a 15-minute-old signal
verbatim:

- **ETH** — reward:risk collapses from 2.00 to **0.81**
- **SOL** — from 2.00 to **0.60**

That is not degradation, it is inversion: you would be taking 2:1 setups at worse than 1:1.

**But the fix is a design change, not a subscription.** `scalp_levels` is a pure function of
`(price, atr_pct, cost_frame)`. The phone can run it. So:

> **Do not ship levels in the notification. Ship the fact that a setup is live.**
> *"ETH — confluence short, 3 of 5 agree"*. When the user opens the app, recompute entry,
> stop, target and expected time from the price *at that moment*.

Delay then costs **optionality** (the setup may have gone) rather than **correctness** (the
levels are always fresh). A 15-minute-late tap is still a good trade or clearly no longer
one, and the app can say which.

This also fixes something the server does badly today: it publishes a price and hopes you
act on it soon.

**When you would still need APNs:** only if you want a guarantee of seeing a setup within
~2 minutes of it forming. Given holds are 1–6 hours and the system is deliberately selective
(≈0.4 signals per symbol per evaluation), missing some is an acceptable cost. Revisit only
if the alert-to-open latency proves to be the thing losing money.

**Recommendation: build Phases 1–5 as planned, stay on free provisioning, and re-evaluate
after two weeks of real use.**

## 10. Where findings contradict the plan

1. **Phase 3's "cooldown/debounce" understates it.** There are three interacting pieces of
   state (§4.3), and the third — the contradiction guard — was added *because* the first two
   were not enough. Budget for all three.

2. **Phase 5's "real captured JSON fixtures" is more important than it reads.** Two of the
   worst bugs in this project's history were parsers silently accepting the wrong shape: a
   rolling 24-hour volume stamped onto every 1-minute bar, and 15-minute archive candles
   loaded into a 1-minute series. Both rendered identically to correct data. Capture fixtures
   from Binance now, and assert on `bar_minutes` and on volume varying between bars.

3. **`AlertEngine` as "pure synchronous evaluation" is already true in the source** —
   `scalp_levels`, `confluence.evaluate`, `forecast_symbol` and `detect_surge` are all pure
   functions over candle arrays with no I/O. This is a straight transliteration, not a
   redesign. Good news for the schedule.

4. **Consider porting the Price Outlook before the signal engine.** Signals answer "is there
   a trade worth its costs right now", and the honest answer is usually no — which leaves the
   screen empty. `forecast_symbol` gives every market a band and a lean at 1h/4h/24h, always.
   It is ~150 lines, has no state, no cooldowns, and no staleness problem, and it would make
   Phase 2 demonstrable on its own.

## 11. Mapping table — source concept → Swift home

| Source | Lines | Swift home | Notes |
|---|---|---|---|
| `collectors/binance_klines.py` | 250 | `Services/MarketDataClient` | Protocol + live impl. Keep the host-fallback list and the "remember what answered" behaviour |
| `analysis/crypto_state.py` | — | `Models/Instrument`, `Models/Candle` | `@Observable`; candles in memory only |
| `analysis/crypto_state_store.py` | — | `Services/MarketStore` | Actor. Owns the 360-bar cap and `replace_candles` semantics |
| `analysis/indicators.py` | 561 | `Services/Indicators` | Pure static funcs. Direct transliteration |
| `analysis/patterns.py` | — | `Services/Patterns` | Returns `Pattern(direction, strength, description)` |
| `analysis/confluence.py` | 450 | `Services/Confluence` | `Vote`/`Verdict` → structs. Keep one-vote-per-family |
| `analysis/scalp_levels.py` | — | `Services/LevelPolicy` | **Port exactly.** `NoTrade` → Swift enum with associated reason |
| `analysis/crypto_signals.py` | — | `Services/Analyzers` | Five types conforming to one protocol |
| `analysis/crypto_engine.py` | — | `Services/AlertEngine` | The stateful part: cooldown, live map, contradiction guard |
| `analysis/forecast.py` | 150 | `Services/Forecaster` | Pure. Feeds a `PriceOutlook` view |
| `analysis/orderbook.py` | — | `Services/OrderBook` | Optional for v1; refines the cost estimate |
| `analysis/instruments.py` | — | `Models/InstrumentSpec` | Per-market fee, tick, maintenance margin |
| `storage/models.py` (3 tables) | — | SwiftData `@Model` | `SignalRecord`, `WatchlistItem` |
| `notifications/crypto_formatter.py` | — | `Services/NotificationBuilder` | **Change the payload — see §9** |
| `scheduler/runner.py` jobs | — | `BGTaskScheduler` + a foreground timer | Only two jobs matter, both 60 s |
| `scheduler/health.py` | 5,163 | `Features/*` SwiftUI | Behaviour reference only |
| `analysis/signal_audit.py` | — | `Features/Accuracy` | Outcome scoring; expectancy over win rate |
| Tennis / football / paper trading | 4,680 | — | **Not ported** |

---

## Phase 0 exit

Stopping here for confirmation, per the working agreement. Three things to decide before
Phase 1:

1. **Accept the §9 recommendation** — notify "setup live", recompute levels on open, stay
   on free provisioning?
2. **Port the Price Outlook first** (§10.4)? It is smaller, stateless, and demos well.
3. **Confirm the watchlist** — eight symbols, or narrow it? Gold (XAU) needs a different
   cost frame and trades on Binance as `PAXGUSDT`, which is worth knowing now rather than in
   Phase 2.
