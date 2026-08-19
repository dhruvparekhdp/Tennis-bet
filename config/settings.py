from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Grand Slams + ATP Masters 1000 + WTA 1000 — keyword fragments matched case-insensitively
# against tournament names from any data source.
TIER1_KEYWORDS: frozenset[str] = frozenset({
    # Grand Slams
    "australian open", "roland garros", "french open", "wimbledon", "us open",
    # ATP Masters 1000
    "indian wells", "miami open", "monte carlo", "madrid open", "monte-carlo",
    "italian open", "internazionali", "canada open", "canadian open",
    "montreal", "toronto", "western & southern", "cincinnati",
    "shanghai", "paris masters", "rolex paris",
    # WTA 1000 (same venues, some different names)
    "china open", "beijing", "guadalajara",
})


def is_tier1(tournament_name: str) -> bool:
    """Return True if the tournament is a Grand Slam or Masters 1000 / WTA 1000."""
    name_lower = tournament_name.lower()
    return any(kw in name_lower for kw in TIER1_KEYWORDS)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Telegram — optional so non-notification modules can import without credentials
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None

    # Strategy
    min_confidence: float = 0.65
    max_stake_pct: float = 0.03
    bank_size: float = 10000.0

    # Polling intervals
    sofascore_poll_interval: int = 30
    schedule_poll_interval: int = 300
    signal_cooldown_minutes: int = 10

    # Storage
    database_url: str = "sqlite+aiosqlite:///./tennis_bet.db"

    # TheSportsDB
    thesportsdb_api_key: str = "3"

    # Odds API
    odds_api_key: str | None = None  # https://the-odds-api.com
    odds_poll_interval_seconds: int = 300  # 5 min default — ~8,640 req/month for 2 sports
    # How far ahead to show upcoming matches (the API itself returns ~24h of fixtures).
    # Widened from 3h so the dashboard isn't empty when nothing is live right now.
    odds_upcoming_window_hours: int = 24
    odds_regions: str = "eu,uk,us"

    # BetsAPI — https://betsapi.com (live scores + in-play odds, cloud-safe)
    bets_api_token: str | None = None

    # Sportradar — https://developer.sportradar.com (free 30-day trial)
    # One call returns ALL live matches across every competition (Challengers, ITF, all football)
    # Trial quota: 1,000 calls/product/30 days
    sportradar_api_key: str | None = None
    sportradar_poll_interval_seconds: int = 300  # 5 min, use /settings toggle to pause

    # API-Sports Tennis — https://api-sports.io (100 req/day FREE, cloud-safe)
    api_sports_key: str | None = None
    api_sports_poll_interval_seconds: int = 900  # 15 min → 96 calls/day

    # SportsData.io Tennis — https://www.sportsdata.io (250 req/day free trial)
    sportsdata_api_key: str | None = None
    sportsdata_poll_interval_seconds: int = 600  # 10 min = 144 calls/day

    # API-Tennis.com — https://api-tennis.com (no hard credit limits)
    api_tennis_key: str | None = None
    api_tennis_poll_interval_seconds: int = 300  # 5 min, no quota restrictions

    # Tournament filter — "tier1" = Slams + Masters 1000/WTA 1000 only, "all" = everything
    tournament_tier: str = "tier1"

    # Master switch for all tennis + football collection. When False, none of the
    # sports polling/analysis jobs are scheduled at all — no API quota is spent and
    # no CPU is used on them. The /sports pages still render (from whatever is in
    # the DB), they just stop receiving new data. Crypto is unaffected.
    sports_enabled: bool = False

    # Push-client ingest — shared secret between your laptop's push_client.py and Render.
    # Set INGEST_API_KEY in Render env vars; pass the same value via --key to push_client.py.
    ingest_api_key: str = ""

    # Scalping / sure-shot detection — thresholds for flagging near-certain in-play winners.
    # A "lock" needs very high model conviction AND very short market odds.
    scalp_min_win_prob: float = 0.90      # surface as a scalp at/above this model win prob
    scalp_lock_win_prob: float = 0.97     # "lock" tier
    scalp_max_odds: float = 1.25          # only consider favourites priced at/below this
    scalp_lock_max_odds: float = 1.10     # "lock" tier max odds
    scalp_alert_telegram: bool = True     # ping Telegram when a new "lock" scalp appears
    scalp_alert_cooldown_minutes: int = 30

    # ── Crypto & Commodities ──────────────────────────────────────────────────
    # The watchlist itself lives in the crypto_watchlist DB table, not here — it's
    # editable at runtime from /settings (Crypto tab) with no redeploy needed.
    # crypto_watchlist_seed is only used once, the first time that table is empty
    # (e.g. a fresh deploy), to give the app something to stream on startup.
    # Kept small deliberately: each symbol is a continuous Binance WS stream plus
    # a DB snapshot row every crypto_snapshot_interval_seconds, and this app runs
    # on Render's free tier (512MB RAM, shared CPU) alongside tennis/football polling.
    crypto_watchlist_seed: str = (
        "btcusdt,ethusdt,bnbusdt,solusdt,xrpusdt,dogeusdt,adausdt,linkusdt,ltcusdt,dotusdt"
    )
    # Active streaming Kline interval
    crypto_kline_interval: str = "1m"
    # Target prediction timeframes
    crypto_timeframes: str = "30m,1h,4h,1d"

    # Binance WebSocket — off by default because the main host returns HTTP 451
    # (geo-blocked) from Render's US IPs. Hit /api/debug/binance on the live
    # server first: it probes every candidate host and tells you whether any
    # work. If one does, set this true — Binance klines carry true OHLC, which
    # makes ATR (and therefore signal target/stop distances) far more realistic
    # than the flat open==high==low==close snapshots REST polling produces.
    binance_ws_enabled: bool = False

    # CoinDCX — https://coindcx.com/api (preferred crypto price source).
    # Public ticker endpoint, no API key, no meaningful rate limit — one call
    # returns every market on the exchange. Preferred over CoinGecko for any
    # symbol it lists, since it's the exact price you'd see trading there.
    coindcx_poll_interval_seconds: int = 30

    # CoinGecko — https://www.coingecko.com/en/api (fallback for anything
    # CoinDCX doesn't list). Binance's WebSocket API returns HTTP 451
    # (geoblocked) from Render's IPs, so it can't be used reliably there — this
    # REST polling replaces it. A free "Demo" key (no credit card) raises the
    # rate limit to 100 calls/min / 10k/month, but isn't required: one poll
    # covers the whole watchlist in a single batched call, well under the
    # unauthenticated limit even at 60s.
    coingecko_api_key: str | None = None
    coingecko_poll_interval_seconds: int = 60

    # Twelve Data Commodities (Gold, Silver, WTI Crude Oil)
    twelvedata_api_key: str | None = None
    twelvedata_symbols: str = "XAU/USD,XAG/USD,WTI/USD"

    # CryptoPanic News & Sentiment — note: as of 2026 CryptoPanic's public API
    # requires a paid plan. Leave the token unset to skip sentiment entirely;
    # everything else keeps working without it.
    cryptopanic_auth_token: str | None = None
    cryptopanic_poll_interval_seconds: int = 300  # 5 minutes

    # Crypto Analysis & Thresholds
    crypto_min_confidence: float = 0.60
    crypto_signal_cooldown_minutes: int = 15
    crypto_snapshot_interval_seconds: int = 120   # 2 minutes snapshot cycle for training
    crypto_alert_telegram: bool = True

    # ── Paper trading simulator ───────────────────────────────────────────
    # Off by default. It places no real orders, but it does write to the
    # database and send alerts, so it should be an explicit choice.
    paper_trading_enabled: bool = False
    paper_starting_wallet: float = 3000.0
    paper_target_wallet: float = 20000.0
    paper_leverage: float = 10.0
    paper_stop_pct_of_margin: float = 0.20      # your fixed 20% risk
    paper_reward_risk: float = 1.0              # your fixed 20% target
    paper_min_confidence: float = 0.70
    paper_max_concurrent: int = 3
    paper_max_hold_minutes: int = 240
    paper_scaled_sizing: bool = True            # Rs500 / Rs1,000 / Rs1,500 ladder
    paper_trailing_enabled: bool = False        # measure on real data before enabling
    # Leverage rises with confidence, capped so liquidation stays 3 ATR away.
    paper_scaled_leverage: bool = False
    paper_max_leverage: float = 25.0
    paper_tick_interval_seconds: int = 30
    # CoinDCX INR futures trade at a premium to spot; refresh this if it drifts.
    paper_usdt_inr: float = 102.0
    paper_alert_telegram: bool = True

    # Fewer trades, each with room to pay: a 5x edge multiple keeps 80% of
    # gross instead of 50%, and no indicator family may argue the other way.
    high_conviction_only: bool = False

    # Public URL to ping so the free instance is never idle for 15 minutes.
    # Render injects RENDER_EXTERNAL_URL itself; this is the manual override.
    self_ping_url: str = ""

    # Free, keyless sentiment inputs that adjust confidence (never fire trades).
    sentiment_feeds_enabled: bool = True
    fear_greed_refresh_minutes: int = 60
    crypto_max_stake_pct: float = 0.02           # 2% max per trade (Kelly capped)

    # NLP Sentiment & Execution toggles
    use_finbert: bool = False                    # False = fast keyword lexicon (low RAM), True = FinBERT (needs ~440MB RAM)
    crypto_auto_execute: bool = False            # Auto-execution hook (prepared for later Binance API execution)

    @property
    def prediction_timeframes(self) -> list[str]:
        """Return list of active prediction timeframes."""
        return [t.strip().lower() for t in self.crypto_timeframes.split(",") if t.strip()]


settings = Settings()  # type: ignore[call-arg]
