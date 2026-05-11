from datetime import datetime

from sqlalchemy import Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from storage.database import Base


class Match(Base):
    __tablename__ = "matches"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # sofascore event id
    player1: Mapped[str] = mapped_column(String)
    player2: Mapped[str] = mapped_column(String)
    tournament: Mapped[str] = mapped_column(String)
    surface: Mapped[str] = mapped_column(String)
    first_seen: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    last_updated: Mapped[datetime] = mapped_column(default=datetime.utcnow)
    is_finished: Mapped[bool] = mapped_column(default=False)


class OddsSnapshot(Base):
    __tablename__ = "odds_snapshots"
    __table_args__ = (Index("ix_odds_match_ts", "match_id", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(String, index=True)
    odds_p1: Mapped[float] = mapped_column(Float)
    odds_p2: Mapped[float] = mapped_column(Float)
    timestamp: Mapped[datetime] = mapped_column(index=True)


class SignalLog(Base):
    __tablename__ = "signal_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(String, index=True)
    signal_type: Mapped[str] = mapped_column(String)
    player_to_back: Mapped[int] = mapped_column(Integer)
    trigger_description: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    recommended_market: Mapped[str] = mapped_column(String)
    current_odds: Mapped[float] = mapped_column(Float)
    fair_odds: Mapped[float] = mapped_column(Float)
    edge_pct: Mapped[float] = mapped_column(Float)
    stake_pct: Mapped[float] = mapped_column(Float)
    timestamp: Mapped[datetime] = mapped_column(index=True)


class PlayerStats(Base):
    """Historical player statistics loaded from Tennis Abstract CSV data."""

    __tablename__ = "player_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_name: Mapped[str] = mapped_column(String, index=True)
    surface: Mapped[str] = mapped_column(String)
    matches_played: Mapped[int] = mapped_column(Integer, default=0)
    matches_won: Mapped[int] = mapped_column(Integer, default=0)
    first_set_losses: Mapped[int] = mapped_column(Integer, default=0)
    first_set_loss_wins: Mapped[int] = mapped_column(Integer, default=0)
    avg_first_serve_pct: Mapped[float] = mapped_column(Float, default=0.60)
    avg_aces_per_game: Mapped[float] = mapped_column(Float, default=0.5)
    avg_dfs_per_game: Mapped[float] = mapped_column(Float, default=0.2)
