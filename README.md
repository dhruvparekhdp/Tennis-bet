# crypto-signal-engine

A real-time crypto market analysis system. It watches a configurable set of
USDT perpetual pairs, runs five independent signal detectors over live
candles, and only acts where independent evidence agrees. Signals are
tracked through a paper-trading simulator (no real capital), scored against
their actual outcomes, and exposed through a web dashboard and a small
authenticated API.

The project began as a tennis-match betting monitor. The sports code is
still in the repository — working, tested, and reachable behind a feature
flag — but it is not part of the running system today. See
[Status: the tennis/football engine](#status-the-tennisfootball-engine)
for why it was kept rather than deleted.

---

## Architecture

```
Binance WebSocket ─┐
CoinDCX REST        ├─► CryptoStateStore ─► five detectors ─► confluence gate ─► level policy
CoinGecko REST      │      (in-memory,        (independent      (≥3 of 5         (cost floor,
CryptoPanic (news) ─┘       per-symbol,        per family,       families,        trailing stop,
                             rolling candle     one vote each)    no direction     reward:risk 2)
                             window)                              on veto)
                                                                        │
                                                                        ▼
                                              cooldown + contradiction rejection
                                                        (CryptoEngine)
                                                                        │
                                    ┌───────────────────────────────────┼──────────────────┐
                                    ▼                                   ▼                  ▼
                          paper-trading simulator              outcome logging      Telegram alert
                          (trailing stop, laddered              (crypto_signal_log)
                           exits, simulated fills)                       │
                                    │                                    ▼
                                    ▼                          signal_audit (win rate,
                          paper_trades / paper_cycles           expectancy, per-family
                                                                 slicing, introspected
                                                                 method catalogue)
```

Two entry points share this pipeline. The live path is driven by APScheduler
jobs polling real exchanges. The backtest path
(`analysis/backtest.py`) walks historical candles through the *same*
`CryptoState`, the *same* detectors, and the *same* paper-trading fill logic
— a backtest result and a live paper cycle differ only in where the candles
came from. Look-ahead is prevented structurally: a signal generated on
candle N can only fill at candle N's close and is only ever resolved against
candle N+1 onward.

### Why confluence, not five independent alerts

The instinct on seeing one detector dominate the signal feed is to add more
detectors. That makes the false-positive rate worse, not better — more
signals at the same accuracy is strictly worse once every trade pays a real
round-trip cost.

Indicators are grouped into six families — trend, momentum, volatility,
volume, structure, pattern — so that RSI, stochastic and MACD, three views
of the same momentum, can't be counted as three independent confirmations.
Each family gets at most one vote, weighted (trend and structure carry more
weight than a single candlestick pattern), and a trade requires at least
three of five voting families to agree. Volatility is not a voting family:
it can veto a setup outright (the market is too quiet to be worth trading)
but it never elects a direction on its own.

### Why levels are derived from cost, not from volatility alone

Every tradeable instrument has a real round-trip cost — exchange fee, spread,
and a slippage buffer — measured per market rather than assumed globally.
Gold's brokerage on CoinDCX is roughly a fifth of a crypto pair's; holding
both to one global floor would refuse profitable gold setups and accept
unprofitable crypto ones. The target for a signal is required to clear a
multiple of that cost floor before it's published at all, and the stop is
sized so the round-trip cost can never dominate the risk being taken. Reward-
to-risk defaults to 2:1 with a trailing stop once the position is ahead,
rather than a symmetric fixed target — a symmetric target at typical costs
requires close to a 60% hit rate just to break even, which the measured hit
rate on live signals didn't clear.

### Order book, as a distinct source

Every other input is a transformation of past prices. The order book is the
one signal that describes what other market participants have committed to
do next, and it's used two ways: to reprice the assumed spread/slippage with
a measurement from the live book instead of a flat estimate, and to veto a
target that has a resting order wall between entry and target large enough
to plausibly stop the move before it arrives. It never elects a direction.

### Cooldown and contradiction rejection

Two separate problems, addressed separately. A cooldown per (symbol, signal
type) stops one detector from re-firing on the same continuous move. A
second, independent check stops the system from holding two *contradictory*
live signals on the same symbol — a confluence long and a volume-spike short
on the same coin, seven minutes apart, both technically real detector
outputs — by refusing a new signal in the opposite direction of one that's
still open, until the open one resolves against its own stop or target.

---

## What's real vs. what the paper trader does

Nothing here places a live exchange order. The paper-trading simulator
(`analysis/paper_trading.py`, `analysis/paper_cycle.py`) models a fixed
starting wallet, leveraged positions, trailing stops, and an optional
laddered exit (locking in partial profit at return-on-equity milestones
before the position resolves). Every open position and every closed trade —
fees, funding, slippage, net P&L — is written to Postgres so the numbers can
be audited later, not just watched live.

## Outcome tracking

`analysis/signal_audit.py` turns the logged signal history into a scoreboard:
win rate, but also net expectancy (win rate alone is a bad measure once
costs are netted in — a coin-flip win rate at 1:1 reward:risk is a loser
after fees), sliced by symbol, setup, time horizon, direction, and
confidence band, so a systematic failure is attributable rather than
guessed at. It also introspects the actual analyzer/indicator source at
request time and serves a live method catalogue alongside the audit table —
the documentation of "what fired this signal" is read from the code, not
maintained by hand, so it can't drift out of sync with it.

There is no machine-learning model retraining on this crypto outcome
history today. See the cleanup note below — a dormant ML retrain job exists
in the scheduler, but it trains a tennis win-probability model on tennis
match data, not a crypto model.

## API and security

The web dashboard and a JSON API are served from one aiohttp process
(`scheduler/health.py`). Read endpoints — price, forecasts, signal history —
are open; they carry no secret and gating them would only break the
dashboard for no benefit. The three endpoints that change server state
(collector toggles, watchlist edits) require a bearer token
(`scheduler/security.py`), checked with a constant-time comparison and
failing closed if unconfigured — an unset token means those endpoints
refuse everything, not that they're silently open. A separate, much
tighter rate-limit bucket applies to the token-verification endpoint
specifically, since its entire job is accepting attempts at a secret.
General API traffic is rate-limited per client IP (read from
`X-Forwarded-For`, since Render terminates TLS in front of the app and the
raw peer address is the load balancer's, not the caller's).

## Companion iOS app

An in-progress native iOS client lives under [`ios-port/`](ios-port/) — not
a separate repo, a source tree meant to be merged into a sibling Xcode
project. It talks to this backend's API rather than reimplementing the
detection logic in Swift, so there is one implementation of the trading
logic, not two that can drift. See `ios-port/SESSION_CONTEXT.md` for the
current state; it is explicitly marked unverified against a real Swift
toolchain as of the last commit that touched it.

---

## Tech stack

| | |
|---|---|
| Language | Python 3.11+ |
| Web / API | aiohttp |
| Scheduling | APScheduler (in-process, 24/7 interval + cron jobs) |
| Database | SQLAlchemy (async) — SQLite locally, Postgres (asyncpg) in production |
| Live data | `websockets` (Binance), `httpx` (CoinDCX, CoinGecko, CryptoPanic REST) |
| Numerics | pandas, pyarrow, scikit-learn (used by the dormant tennis model — see below) |
| Logging | structlog |
| Notifications | python-telegram-bot |
| Tests | pytest (586 tests as of this write-up) |
| Deployment | Docker, deployed on Render (free tier) |

## Running locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env   # fill in what you need — see Configuration below
python main.py
```

Health check: `curl http://localhost:8080/health`
Dashboard: `http://localhost:8080/`

Run the tests:

```bash
python -m pytest tests -q
```

## Deployment

Containerised (`Dockerfile`), deployed to Render via `render.yaml`. The
health check hits `/health`. `PORT` is read from the environment (Render
sets it); it falls back to `8080` locally.

## Configuration

Settings are environment variables, loaded via `pydantic-settings`
(`config/settings.py`). A non-exhaustive list of what actually matters for
the crypto path:

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | — | Signal and trade alerts |
| `DATABASE_URL` | local SQLite | Postgres URL in production |
| `SPORTS_ENABLED` | `false` | Master switch for the tennis/football engine — see below |
| `PAPER_TRADING_ENABLED` | `false` | Runs the simulated trading cycle |
| `PAPER_LEVERAGE` / `PAPER_STOP_PCT_OF_MARGIN` / `PAPER_REWARD_RISK` | `10` / `0.20` / `2.0` | Paper position sizing and risk |
| `API_AUTH_TOKEN` | unset (endpoints refuse) | Bearer token for the three mutating API endpoints |
| `SENTIMENT_INGEST_TOKEN` | unset (endpoint refuses) | Shared secret for external news-sentiment ingestion |
| `BINANCE_KLINES_ENABLED` | `true` | Live candle source |
| `COINGECKO_API_KEY` | unset | Optional — raises the CoinGecko rate limit |

Full list in `config/settings.py`.

## Project structure

```
crypto-signal-engine/
├── main.py                       # entry point
├── config/settings.py            # all configuration
├── collectors/
│   ├── binance_klines.py         # live candles, primary crypto data source
│   ├── coindcx.py, coingecko.py  # price/ticker sources
│   ├── sentiment_feeds.py        # CryptoPanic news sentiment
│   └── ...                       # sofascore.py, api_tennis.py, etc. — dormant, sports_enabled-gated
├── analysis/
│   ├── crypto_state.py, crypto_state_store.py   # canonical live market state
│   ├── indicators.py             # RSI, MACD, Bollinger, ATR, volume metrics, etc.
│   ├── confluence.py             # family voting + volatility veto
│   ├── scalp_levels.py           # cost-floor-derived target/stop/reward:risk
│   ├── crypto_signals.py         # the five detectors + the confluence detector
│   ├── crypto_engine.py          # cooldown + contradiction rejection
│   ├── orderbook.py              # book-derived cost and wall-veto
│   ├── paper_trading.py, paper_cycle.py         # simulated execution
│   ├── backtest.py               # historical replay through the same pipeline
│   ├── signal_audit.py           # outcome scoring + live method catalogue
│   └── ml_predictor.py           # dormant — trains a TENNIS model, not crypto (see below)
├── scheduler/
│   ├── runner.py                 # APScheduler job registration and orchestration
│   ├── health.py                 # aiohttp app: dashboard + JSON API
│   └── security.py               # bearer auth, rate limiting, security headers
├── storage/                      # SQLAlchemy models + repository
├── notifications/                # Telegram formatting and delivery
├── ios-port/                     # in-progress native iOS client (see above)
└── tests/                        # 586 tests
```

---

## Status: the tennis/football engine

The original build. It's complete and was working — live match polling
(Sofascore, ESPN, Flashscore, Sportradar, API-Tennis, football odds APIs),
five tennis-specific signal analyzers (momentum shift, odds overreaction,
serve degradation, set patterns, fatigue), a Markov-chain plus
logistic-regression win-probability model, and its own Telegram alert
format. All of it is reachable by setting `SPORTS_ENABLED=true`; with it
unset, `scheduler/runner.py` never registers a single sports job — no API
quota spent, no CPU spent, and the crypto path is unaffected either way.

It was kept rather than deleted because it's tested, working code, and
ripping it out is a separate, deliberate decision rather than a side effect
of a documentation pass. It's flagged here, explicitly, so it reads as an
intentional dormant subsystem rather than as evidence the repository wasn't
cleaned up.

## Known inaccuracy to fix, not just a naming issue

The `ml_retrain` job runs unconditionally, every six hours, regardless of
`SPORTS_ENABLED`. It calls `MLPredictor.maybe_retrain`, which loads training
data from `MatchResult` — sets, games, serve percentage, surface — and fits
a logistic regression to predict a *tennis match winner*. It is not a crypto
model, it does not train on `crypto_signal_log`, and with sports disabled it
has no fresh data to train on. If a crypto outcome model is wanted, it needs
to be built against `crypto_signal_log`/`signal_audit`; the existing
`ml_predictor.py` is unrelated tennis code that happens to still run on a
timer. Recommend either building the crypto version or disabling/removing
the job until it exists, so "the system retrains a model every six hours"
is a true statement about what's deployed.
