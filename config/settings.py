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
    # Top 50 default coins watchlist (comma-separated, editable via .env CRYPTO_WATCHLIST)
    crypto_watchlist: str = (
        "btcusdt,ethusdt,bnbusdt,solusdt,xrpusdt,dogeusdt,adausdt,avaxusdt,shibusdt,dotusdt,"
        "linkusdt,trxusdt,nearusdt,suiusdt,aptusdt,uniusdt,ltcusdt,pepeusdt,fetusdt,renderusdt,"
        "icpusdt,bchusdt,kasusdt,polusdt,etcusdt,xlmusdt,taousdt,injusdt,stxusdt,filusdt,"
        "imxusdt,arbusdt,vetusdt,seiusdt,ftmusdt,runeusdt,flokiusdt,bonkusdt,wifusdt,grtusdt,"
        "aaveusdt,algousdt,sandusdt,manausdt,flowusdt,thetausdt,egldusdt,qntusdt,axsusdt,galausdt"
    )
    # Active streaming Kline intervals (default "1m", "5m", "15m", "1h")
    crypto_kline_interval: str = "1m"
    # Target prediction timeframes
    crypto_timeframes: str = "30m,1h,4h,1d"

    # Twelve Data Commodities (Gold, Silver, WTI Crude Oil)
    twelvedata_api_key: str | None = None
    twelvedata_symbols: str = "XAU/USD,XAG/USD,WTI/USD"

    # CryptoPanic News & Sentiment
    cryptopanic_auth_token: str | None = None
    cryptopanic_poll_interval_seconds: int = 300  # 5 minutes

    # Crypto Analysis & Thresholds
    crypto_min_confidence: float = 0.60
    crypto_signal_cooldown_minutes: int = 15
    crypto_snapshot_interval_seconds: int = 120   # 2 minutes snapshot cycle for training
    crypto_alert_telegram: bool = True
    crypto_max_stake_pct: float = 0.02           # 2% max per trade (Kelly capped)

    # NLP Sentiment & Execution toggles
    use_finbert: bool = False                    # False = fast VADER/lexicon (low RAM), True = FinBERT (needs ~440MB RAM)
    crypto_auto_execute: bool = False            # Auto-execution hook (prepared for later Binance API execution)

    @property
    def crypto_symbols(self) -> list[str]:
        """Return clean list of lowercase symbols from the watchlist."""
        return [s.strip().lower() for s in self.crypto_watchlist.split(",") if s.strip()]

    @property
    def prediction_timeframes(self) -> list[str]:
        """Return list of active prediction timeframes."""
        return [t.strip().lower() for t in self.crypto_timeframes.split(",") if t.strip()]


settings = Settings()  # type: ignore[call-arg]
