from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from storage.database import Base


class Match(Base):
    __tablename__ = "matches"

    id: Mapped[str] = mapped_column(String, primary_key=True)
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
    player_name: Mapped[str] = mapped_column(String, default="")
    opponent_name: Mapped[str] = mapped_column(String, default="")
    tournament: Mapped[str] = mapped_column(String, default="")
    surface: Mapped[str] = mapped_column(String, default="hard")
    trigger_description: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float)
    recommended_market: Mapped[str] = mapped_column(String)
    current_odds: Mapped[float] = mapped_column(Float)
    fair_odds: Mapped[float] = mapped_column(Float)
    edge_pct: Mapped[float] = mapped_column(Float)
    stake_pct: Mapped[float] = mapped_column(Float)
    # Model state at signal time
    model_win_prob: Mapped[float] = mapped_column(Float, default=0.0)
    score_at_signal: Mapped[str] = mapped_column(String, default="")   # e.g. "1-0, 3-2"
    sets_p1_at_signal: Mapped[int] = mapped_column(Integer, default=0)
    sets_p2_at_signal: Mapped[int] = mapped_column(Integer, default=0)
    games_p1_at_signal: Mapped[int] = mapped_column(Integer, default=0)
    games_p2_at_signal: Mapped[int] = mapped_column(Integer, default=0)
    # Filled in when match completes
    outcome: Mapped[str] = mapped_column(String, default="pending")    # pending/won/lost/void
    match_winner: Mapped[int] = mapped_column(Integer, default=0)      # 1 or 2, 0 = unknown
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


class MatchSnapshot(Base):
    """
    Periodic snapshot of live match state — primary source for ML training.
    One row every ~2 minutes per match while live.
    winner column is NULL during play, filled in retroactively when match completes.
    """

    __tablename__ = "match_snapshots"
    __table_args__ = (Index("ix_snap_match_ts", "match_id", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(String, index=True)
    player1_name: Mapped[str] = mapped_column(String)
    player2_name: Mapped[str] = mapped_column(String)
    surface: Mapped[str] = mapped_column(String)
    tournament: Mapped[str] = mapped_column(String)
    # Score state
    sets_p1: Mapped[int] = mapped_column(Integer)
    sets_p2: Mapped[int] = mapped_column(Integer)
    games_p1: Mapped[int] = mapped_column(Integer)
    games_p2: Mapped[int] = mapped_column(Integer)
    current_set: Mapped[int] = mapped_column(Integer)
    total_games_played: Mapped[int] = mapped_column(Integer)
    # Momentum: positive = p1 winning streak, negative = p2 winning streak
    p1_momentum: Mapped[int] = mapped_column(Integer, default=0)
    # Odds
    odds_p1: Mapped[float] = mapped_column(Float)
    odds_p2: Mapped[float] = mapped_column(Float)
    # Model predictions at this moment
    model_win_prob_p1: Mapped[float] = mapped_column(Float, default=0.0)
    model_win_prob_p2: Mapped[float] = mapped_column(Float, default=0.0)
    # Serve stats (0.0 if unavailable)
    serve_pct_p1: Mapped[float] = mapped_column(Float, default=0.0)
    serve_pct_p2: Mapped[float] = mapped_column(Float, default=0.0)
    # Full game sequence as JSON array, e.g. [1,2,1,1,2]
    game_log_json: Mapped[str] = mapped_column(Text, default="[]")
    # Outcome — NULL until match completes, then set to 1 or 2
    winner: Mapped[int] = mapped_column(Integer, default=0)    # 0 = not yet known
    timestamp: Mapped[datetime] = mapped_column(DateTime, index=True)


class MatchCompletion(Base):
    """
    Final result of a match — used to label MatchSnapshot and SignalLog rows.
    Created when a match disappears from the live feed.
    """

    __tablename__ = "match_completions"

    match_id: Mapped[str] = mapped_column(String, primary_key=True)
    player1_name: Mapped[str] = mapped_column(String)
    player2_name: Mapped[str] = mapped_column(String)
    winner: Mapped[int] = mapped_column(Integer)               # 1 or 2
    final_sets_p1: Mapped[int] = mapped_column(Integer)
    final_sets_p2: Mapped[int] = mapped_column(Integer)
    final_score_str: Mapped[str] = mapped_column(String)       # "6-3, 7-5"
    tournament: Mapped[str] = mapped_column(String)
    surface: Mapped[str] = mapped_column(String)
    total_games: Mapped[int] = mapped_column(Integer)
    total_signals_fired: Mapped[int] = mapped_column(Integer, default=0)
    signals_correct: Mapped[int] = mapped_column(Integer, default=0)
    completed_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class MatchResult(Base):
    """Training data row for the ML win predictor, recorded at match completion."""

    __tablename__ = "match_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(String, index=True)
    p1_sets_lead: Mapped[int] = mapped_column(Integer)
    p1_games_lead: Mapped[int] = mapped_column(Integer)
    current_set: Mapped[int] = mapped_column(Integer)
    p1_momentum: Mapped[int] = mapped_column(Integer)
    p1_serve_pct: Mapped[float] = mapped_column(Float)
    p2_serve_pct: Mapped[float] = mapped_column(Float)
    surface_clay: Mapped[int] = mapped_column(Integer)
    surface_grass: Mapped[int] = mapped_column(Integer)
    surface_indoor: Mapped[int] = mapped_column(Integer)
    match_progress: Mapped[float] = mapped_column(Float)
    p1_opening_implied: Mapped[float] = mapped_column(Float)
    winner: Mapped[int] = mapped_column(Integer)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
