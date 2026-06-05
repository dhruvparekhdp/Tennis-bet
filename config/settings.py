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

    # BetsAPI — https://betsapi.com (live scores + in-play odds, cloud-safe)
    bets_api_token: str | None = None

    # Sportradar — https://developer.sportradar.com (free 30-day trial)
    # One call returns ALL live matches across every competition (Challengers, ITF, all football)
    # Trial quota: 1,000 calls/product/30 days — poll every 2 min = ~720 calls/30 days
    sportradar_api_key: str | None = None
    sportradar_poll_interval_seconds: int = 120  # 2 min → stays within trial quota

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


settings = Settings()  # type: ignore[call-arg]
