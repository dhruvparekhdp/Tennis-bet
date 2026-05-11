from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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


settings = Settings()  # type: ignore[call-arg]
