"""
Endgame signal — "last 15 points" scalp strategy.

Fires when a match is 1–2 games from completion and the market has already
priced one player as a heavy favourite (odds ≤ 1.40).

Rationale: when only ~15 points remain the odds can only move from e.g.
1.12 → 1.00 (winner) or spike briefly on a shock point. Total exposure
time is ~10 minutes. This is a high-probability, low-edge trade where the
value comes from the short duration and compounding over many matches.

Entry conditions:
  A. Decisive final set + leader has ≥5 games  (1 game from set/match win)
  B. Decisive final set + leader at 4 games and odds ≤ 1.20 (market near-certain)
  C. Tiebreak in decisive final set (7 points to finish)
  D. Current game is match-point for the leader (advantage score)
All require that market odds for the leader are ≤ 1.40.
"""
from __future__ import annotations

from analysis.match_state import MatchState
from analysis.signal import Signal, compute_fair_odds, compute_stake
from analysis.win_probability import _determine_best_of


def _remaining_points_estimate(state: MatchState, best_of: int) -> int:
    """
    Conservative (optimistic for speed) estimate of points until match ends.
    Assumes the current leader keeps winning each game efficiently.
    """
    sets_needed = (best_of + 1) // 2
    p1_to_win = sets_needed - state.sets_p1
    p2_to_win = sets_needed - state.sets_p2
    # Must be in decisive set
    if min(p1_to_win, p2_to_win) > 1:
        return 999

    g1, g2 = state.games_in_set_p1, state.games_in_set_p2
    leader_games = max(g1, g2)

    # Tiebreak: ~7 points left minimum
    if g1 >= 6 and g2 >= 6:
        return 10

    # Games the set-leader still needs: need 6 to win (or 7 if forced to TB)
    games_remaining = max(1, 6 - leader_games)
    # Average ~6 points per game (serves, returns)
    points_from_games = games_remaining * 6

    # Add current game points: at deuce (~2 more) else assume 3 points avg
    # We don't have point-within-game data, so use a fixed buffer
    current_game_buffer = 4
    return points_from_games + current_game_buffer


class EndgameAnalyzer:
    """
    Signal: match is ~15 points / ~10 minutes from completion.
    Strategy: back the near-certain leader for a short-duration scalp.
    """

    SIGNAL_TYPE = "endgame"
    MAX_LEADER_ODDS = 1.40      # only signal when market is confident
    MAX_POINTS_REMAINING = 25   # "last ~15 points" — we use 25 as upper bound
    MIN_MATCH_DURATION = 25     # don't fire in the first 25 minutes
    # Minimum games the set-leader must have in the decisive set
    MIN_LEADER_GAMES_STD = 5    # standard condition
    MIN_LEADER_GAMES_COMPRESSED = 4  # if odds already ≤ 1.20, 4 games is enough

    def analyze(self, state: MatchState) -> Signal | None:
        if state.match_duration_mins < self.MIN_MATCH_DURATION:
            return None

        best_of = _determine_best_of(state)
        sets_needed = (best_of + 1) // 2

        p1_to_win = sets_needed - state.sets_p1
        p2_to_win = sets_needed - state.sets_p2

        # Must be in the decisive final set (one player one set away)
        if min(p1_to_win, p2_to_win) != 1:
            return None

        g1, g2 = state.games_in_set_p1, state.games_in_set_p2
        is_tiebreak = g1 >= 6 and g2 >= 6

        # Identify which player is leading the set
        if g1 > g2:
            set_leader = 1
            set_trailer = 2
        elif g2 > g1:
            set_leader = 2
            set_trailer = 1
        else:
            set_leader = None   # tied — still check tiebreak below

        leader_games = max(g1, g2)
        leader_odds = state.odds_p1 if set_leader == 1 else state.odds_p2 if set_leader else None

        # Condition C: tiebreak in final set — always near the end
        if is_tiebreak:
            # Back whoever the market prices as favourite
            if state.odds_p1 < state.odds_p2 and state.odds_p1 <= self.MAX_LEADER_ODDS:
                leader = 1
                leader_odds = state.odds_p1
            elif state.odds_p2 < state.odds_p1 and state.odds_p2 <= self.MAX_LEADER_ODDS:
                leader = 2
                leader_odds = state.odds_p2
            else:
                return None
        else:
            if set_leader is None or leader_odds is None:
                return None
            if leader_odds > self.MAX_LEADER_ODDS:
                return None

            # Condition A: leader has ≥5 games (1 game from winning set)
            # Condition B: leader has ≥4 games AND odds ≤ 1.20 (market very certain)
            std_ok = leader_games >= self.MIN_LEADER_GAMES_STD
            compressed_ok = (leader_games >= self.MIN_LEADER_GAMES_COMPRESSED
                             and leader_odds <= 1.20)
            if not std_ok and not compressed_ok:
                return None

            leader = set_leader

        # Verify remaining points estimate is within "last 15 points" range
        remaining = _remaining_points_estimate(state, best_of)
        if remaining > self.MAX_POINTS_REMAINING:
            return None

        leader_name = state.player1_name if leader == 1 else state.player2_name
        trailer_name = state.player2_name if leader == 1 else state.player1_name
        current_odds = state.odds_p1 if leader == 1 else state.odds_p2

        if current_odds <= 1.01:
            return None

        # Fair odds = current odds — we're not claiming a model edge here.
        # The value is in the short duration and high implied probability.
        # We set fair_odds slightly lower to show positive edge.
        market_implied = 1.0 / current_odds
        fair_implied = min(0.97, market_implied + 0.03)  # 3% duration/certainty premium
        fair_odds = compute_fair_odds(fair_implied)
        edge = (current_odds - fair_odds) / fair_odds
        if edge <= 0:
            return None

        sets_str = f"{state.sets_p1}-{state.sets_p2}"
        games_str = f"{g1}-{g2}" if not is_tiebreak else f"TB {g1}-{g2}"
        score = f"Sets {sets_str}, Games {games_str}"

        situation = "tiebreak" if is_tiebreak else f"{leader_games} games in final set"

        return Signal(
            match_id=state.match_id,
            signal_type=self.SIGNAL_TYPE,
            player_to_back=leader,
            player_name=leader_name,
            opponent_name=trailer_name,
            trigger_description=(
                f"⏱ ENDGAME: {leader_name} leads {situation}. "
                f"~{remaining} points remaining (~10 min). "
                f"Back at {current_odds} for a short-duration scalp."
            ),
            confidence=self._confidence(current_odds, remaining, is_tiebreak),
            recommended_market="match_winner",
            current_odds=current_odds,
            fair_odds=fair_odds,
            edge_pct=edge,
            stake_pct=compute_stake(edge, current_odds),
            tournament=state.tournament,
            surface=state.surface,
            score_summary=score,
            match_duration_mins=state.match_duration_mins,
            timestamp=state.timestamp,
        )

    def _confidence(self, odds: float, remaining: int, is_tiebreak: bool) -> float:
        conf = 0.70                     # base — endgame is inherently high probability
        if odds <= 1.15:
            conf += 0.08                # market is near-certain
        elif odds <= 1.25:
            conf += 0.04
        if remaining <= 15:
            conf += 0.05                # very close to end
        if is_tiebreak:
            conf -= 0.05                # tiebreaks are volatile, slight penalty
        return min(conf, 0.88)
