from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from analysis.football_state import FootballMatchState


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _win_prob(minute: int, goal_diff: int, red_card_adv: int = 0) -> float:
    """
    Estimate win probability for the leading team.
    goal_diff: positive value (we pass abs() from callers).
    red_card_adv: net extra players for the team we're evaluating.
    """
    if goal_diff == 0:
        return 0.50
    time_pct = min(minute / 90.0, 1.0)
    if goal_diff >= 3:
        base = 0.97
    elif goal_diff == 2:
        base = 0.88 + time_pct * 0.08        # 0.88→0.96 over 90'
    else:
        base = 0.62 + time_pct * 0.25        # 0.62→0.87 over 90'

    if red_card_adv != 0:
        base = base + red_card_adv * 0.08
    return round(max(0.05, min(0.97, base)), 4)


def _quarter_kelly(edge: float, odds: float) -> float:
    if edge <= 0 or odds <= 1.0:
        return 0.0
    kelly = edge / (odds - 1.0)
    return round(min(kelly * 0.25, 0.03), 4)


# ── Signal dataclass ───────────────────────────────────────────────────────────

@dataclass
class FootballSignal:
    match_id: str
    signal_type: str
    team_to_back: str
    opponent: str
    tournament: str
    is_home: bool
    market: str               # "match_winner" | "draw_no_bet"
    current_odds: float
    fair_odds: float
    edge_pct: float
    confidence: float
    stake_pct: float
    trigger_description: str
    score_summary: str
    minute: int
    timestamp: datetime


# ── Analyzers ──────────────────────────────────────────────────────────────────

class LateLead:
    """2+ goal lead with <20 minutes left."""

    MIN_MINUTE = 70
    MIN_DIFF = 2

    def analyze(self, state: FootballMatchState) -> FootballSignal | None:
        if state.minute < self.MIN_MINUTE:
            return None
        diff = abs(state.goal_diff)
        if diff < self.MIN_DIFF:
            return None

        is_home = state.goal_diff > 0
        market_odds = state.home_odds if is_home else state.away_odds
        if market_odds <= 1.01:
            return None

        red_adv = (state.away_red_cards - state.home_red_cards) if is_home else (state.home_red_cards - state.away_red_cards)
        prob = _win_prob(state.minute, diff, red_adv)
        fair_odds = round(1.0 / prob, 3)
        edge = prob - 1.0 / market_odds
        if edge < 0.03:
            return None

        confidence = round(min(0.65 + (diff - 2) * 0.05 + (state.minute - 70) * 0.005, 0.88), 3)
        team = state.home_team if is_home else state.away_team
        opp = state.away_team if is_home else state.home_team

        return FootballSignal(
            match_id=state.match_id,
            signal_type="late_lead",
            team_to_back=team,
            opponent=opp,
            tournament=state.tournament,
            is_home=is_home,
            market="match_winner",
            current_odds=market_odds,
            fair_odds=fair_odds,
            edge_pct=round(edge, 4),
            confidence=confidence,
            stake_pct=_quarter_kelly(edge, market_odds),
            trigger_description=(
                f"Leading {state.home_score}:{state.away_score} at {state.minute}' "
                f"— model gives {prob:.0%} win prob vs market implied {1/market_odds:.0%}"
            ),
            score_summary=f"{state.home_score}:{state.away_score} ({state.minute}')",
            minute=state.minute,
            timestamp=state.timestamp,
        )


class HeavyFavoriteDominating:
    """Pre-match favourite (<1.55) leading after half-time."""

    MAX_FAV_ODDS = 1.55

    def analyze(self, state: FootballMatchState) -> FootballSignal | None:
        if state.minute < 45 and not state.is_halftime:
            return None
        if state.goal_diff == 0:
            return None

        is_home = state.goal_diff > 0
        market_odds = state.home_odds if is_home else state.away_odds
        if market_odds <= 1.01:
            return None

        # Check earliest odds snapshot for pre-match context
        if state.odds_history:
            first = state.odds_history[0]
            start_odds = first.home_odds if is_home else first.away_odds
        else:
            start_odds = market_odds

        if start_odds > self.MAX_FAV_ODDS or start_odds <= 1.01:
            return None

        prob = _win_prob(state.minute, abs(state.goal_diff))
        fair_odds = round(1.0 / prob, 3)
        edge = prob - 1.0 / market_odds
        if edge < 0.02:
            return None

        confidence = round(min(0.66 + abs(state.goal_diff) * 0.04 + (state.minute - 45) * 0.002, 0.82), 3)
        team = state.home_team if is_home else state.away_team
        opp = state.away_team if is_home else state.home_team

        return FootballSignal(
            match_id=state.match_id,
            signal_type="heavy_fav_dominating",
            team_to_back=team,
            opponent=opp,
            tournament=state.tournament,
            is_home=is_home,
            market="match_winner",
            current_odds=market_odds,
            fair_odds=fair_odds,
            edge_pct=round(edge, 4),
            confidence=confidence,
            stake_pct=_quarter_kelly(edge, market_odds),
            trigger_description=(
                f"Pre-match favourite {team} (opened {start_odds:.2f}) leading "
                f"{state.home_score}:{state.away_score} at {state.minute}'"
            ),
            score_summary=f"{state.home_score}:{state.away_score} ({state.minute}')",
            minute=state.minute,
            timestamp=state.timestamp,
        )


class LateDrawFade:
    """Clear favourite still drawing with <10 minutes left — back them on DNB."""

    MIN_MINUTE = 80
    MAX_FAV_ODDS = 2.20

    def analyze(self, state: FootballMatchState) -> FootballSignal | None:
        if state.minute < self.MIN_MINUTE:
            return None
        if state.goal_diff != 0:
            return None
        if state.home_odds <= 1.01 or state.away_odds <= 1.01:
            return None

        is_home_fav = state.home_odds < state.away_odds
        fav_odds = state.home_odds if is_home_fav else state.away_odds
        if fav_odds > self.MAX_FAV_ODDS:
            return None

        # At 80+ tied: favourite wins ~30-40% (needs a goal in last 10 min)
        time_rem = max(1, 90 - state.minute)
        prob = min(0.42, 0.28 + (10 - time_rem) * 0.014)
        fair_odds = round(1.0 / prob, 3)
        edge = prob - 1.0 / fav_odds
        if edge < 0.02:
            return None

        confidence = round(min(0.64 + (state.minute - 80) * 0.005, 0.72), 3)
        team = state.home_team if is_home_fav else state.away_team
        opp = state.away_team if is_home_fav else state.home_team

        return FootballSignal(
            match_id=state.match_id,
            signal_type="late_draw_fade",
            team_to_back=team,
            opponent=opp,
            tournament=state.tournament,
            is_home=is_home_fav,
            market="draw_no_bet",
            current_odds=fav_odds,
            fair_odds=fair_odds,
            edge_pct=round(edge, 4),
            confidence=confidence,
            stake_pct=_quarter_kelly(edge, fav_odds),
            trigger_description=(
                f"0:0 at {state.minute}' — favourite {team} ({fav_odds:.2f}) still level, "
                f"market underpricing last-gasp winner"
            ),
            score_summary=f"0:0 ({state.minute}')",
            minute=state.minute,
            timestamp=state.timestamp,
        )


class RedCardAdvantage:
    """One team has a man advantage due to red card(s)."""

    def analyze(self, state: FootballMatchState) -> FootballSignal | None:
        # net advantage for home team
        home_net = state.away_red_cards - state.home_red_cards
        if home_net == 0:
            return None

        is_home = home_net > 0
        adv = abs(home_net)
        market_odds = state.home_odds if is_home else state.away_odds
        if market_odds <= 1.01:
            return None

        score_adv = abs(state.goal_diff) if (state.goal_diff > 0) == is_home else -abs(state.goal_diff)
        prob = _win_prob(state.minute, max(0, score_adv), adv)
        prob = min(0.92, prob)
        fair_odds = round(1.0 / prob, 3)
        edge = prob - 1.0 / market_odds
        if edge < 0.03:
            return None

        confidence = round(min(0.68 + adv * 0.06 + (state.minute / 90) * 0.05, 0.85), 3)
        team = state.home_team if is_home else state.away_team
        opp = state.away_team if is_home else state.home_team
        rc_info = f"{adv} red card{'s' if adv > 1 else ''}"

        return FootballSignal(
            match_id=state.match_id,
            signal_type="red_card_advantage",
            team_to_back=team,
            opponent=opp,
            tournament=state.tournament,
            is_home=is_home,
            market="match_winner",
            current_odds=market_odds,
            fair_odds=fair_odds,
            edge_pct=round(edge, 4),
            confidence=confidence,
            stake_pct=_quarter_kelly(edge, market_odds),
            trigger_description=(
                f"{opp} down to {11 - adv} men ({rc_info}) — "
                f"{team} with numerical advantage at {state.minute}' "
                f"({state.home_score}:{state.away_score})"
            ),
            score_summary=f"{state.home_score}:{state.away_score} ({state.minute}')",
            minute=state.minute,
            timestamp=state.timestamp,
        )


class CleanSheetLikely:
    """
    1-goal lead at 65+ minute AND the losing team's odds have drifted 15%+
    (market is giving up on them).
    """

    MIN_MINUTE = 65
    MIN_DRIFT = 0.15

    def analyze(self, state: FootballMatchState) -> FootballSignal | None:
        if state.minute < self.MIN_MINUTE:
            return None
        if abs(state.goal_diff) != 1:
            return None
        if len(state.odds_history) < 3:
            return None

        is_home = state.goal_diff > 0
        losing_now = state.away_odds if is_home else state.home_odds
        losing_start = (state.odds_history[0].away_odds if is_home
                        else state.odds_history[0].home_odds)
        if losing_start <= 1.01:
            return None

        drift = (losing_now - losing_start) / losing_start
        if drift < self.MIN_DRIFT:
            return None

        prob = _win_prob(state.minute, 1)
        fair_odds = round(1.0 / prob, 3)
        market_odds = state.home_odds if is_home else state.away_odds
        if market_odds <= 1.01:
            return None

        edge = prob - 1.0 / market_odds
        if edge < 0.02:
            return None

        confidence = round(min(0.65 + drift * 0.20 + (state.minute - 65) * 0.003, 0.78), 3)
        team = state.home_team if is_home else state.away_team
        opp = state.away_team if is_home else state.home_team

        return FootballSignal(
            match_id=state.match_id,
            signal_type="clean_sheet_likely",
            team_to_back=team,
            opponent=opp,
            tournament=state.tournament,
            is_home=is_home,
            market="match_winner",
            current_odds=market_odds,
            fair_odds=fair_odds,
            edge_pct=round(edge, 4),
            confidence=confidence,
            stake_pct=_quarter_kelly(edge, market_odds),
            trigger_description=(
                f"{opp} odds drifted {drift:.0%} since kick-off — "
                f"market writing them off; {team} holding {state.home_score}:{state.away_score} at {state.minute}'"
            ),
            score_summary=f"{state.home_score}:{state.away_score} ({state.minute}')",
            minute=state.minute,
            timestamp=state.timestamp,
        )
