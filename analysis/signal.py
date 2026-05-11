from dataclasses import dataclass
from datetime import datetime

from config.settings import settings


@dataclass
class Signal:
    match_id: str
    signal_type: str                # "momentum" | "odds_value" | "serve_degradation" | "set_pattern" | "fatigue"
    player_to_back: int             # 1 or 2
    player_name: str
    opponent_name: str
    trigger_description: str
    confidence: float               # 0.0–1.0
    recommended_market: str         # "match_winner" | "next_game" | "next_set"
    current_odds: float
    fair_odds: float
    edge_pct: float
    stake_pct: float                # fraction of bank (quarter-Kelly, capped)
    tournament: str
    surface: str
    score_summary: str              # e.g. "3-6, 2-2"
    match_duration_mins: int
    timestamp: datetime


def compute_stake(edge_pct: float, odds: float) -> float:
    """Quarter-Kelly stake fraction, hard-capped at settings.max_stake_pct."""
    b = odds - 1.0
    if b <= 0 or edge_pct <= 0:
        return 0.0
    kelly = edge_pct / b
    quarter_kelly = kelly * 0.25
    return min(quarter_kelly, settings.max_stake_pct)


def compute_fair_odds(implied_prob: float) -> float:
    if implied_prob <= 0:
        return 999.0
    return round(1.0 / implied_prob, 2)
