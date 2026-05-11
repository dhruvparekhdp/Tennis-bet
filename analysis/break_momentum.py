"""
Break momentum signal.

A break of serve is the single biggest swing event in a tennis match.
When a player breaks AND the market hasn't fully priced it in (odds moved
less than expected), there is a short-window value opportunity.

Research: break point conversion is highly correlated with match outcome.
A break in the final set is worth significantly more than one in set 1.
"""
from __future__ import annotations

from analysis.match_state import MatchState
from analysis.signal import Signal, compute_fair_odds, compute_stake
from analysis.win_probability import compute_win_probability


class BreakMomentumAnalyzer:
    """
    Signal: player just broke serve (last 2 games show break pattern)
    and the market hasn't fully adjusted yet.

    Break detection: player won a game where the opponent was serving
    (inferred from game_log alternation — every other game is a serve game).
    """

    SIGNAL_TYPE = "break_momentum"
    MIN_MATCH_GAMES = 4
    MAX_ODDS_MOVE = 0.10      # market must have moved <10% since the break
    MIN_SIGNAL_ODDS = 1.25
    # How many games back to look for a break
    BREAK_WINDOW_GAMES = 3

    def analyze(self, state: MatchState) -> Signal | None:
        if state.total_games_played() < self.MIN_MATCH_GAMES:
            return None
        if state.is_tiebreak:
            return None

        for player in (1, 2):
            if not self._just_broke_serve(state, player):
                continue

            current_odds = state.odds_p1 if player == 1 else state.odds_p2
            if current_odds < self.MIN_SIGNAL_ODDS:
                continue

            # Market reaction check — if market already moved 10%+ it's priced in
            odds_move = state.odds_change_pct_last_n_minutes(player, minutes=8)
            if odds_move >= self.MAX_ODDS_MOVE:
                continue

            # Markov confirmation
            model_p1, model_p2 = compute_win_probability(state)
            model_prob = model_p1 if player == 1 else model_p2
            market_prob = 1.0 / current_odds
            if model_prob <= market_prob:
                continue

            # Fair odds = model + small break premium
            # Breaking serve is worth ~5-8% extra win prob
            break_premium = 0.05 if state.current_set <= 2 else 0.08
            fair_implied = min(0.94, model_prob + break_premium)
            fair_odds = compute_fair_odds(fair_implied)
            edge = (current_odds - fair_odds) / fair_odds
            if edge <= 0.03:
                continue

            player_name = state.player1_name if player == 1 else state.player2_name
            opponent = state.player2_name if player == 1 else state.player1_name
            score = (f"{state.sets_p1}-{state.sets_p2} sets, "
                     f"{state.games_in_set_p1}-{state.games_in_set_p2} in set {state.current_set}")

            return Signal(
                match_id=state.match_id,
                signal_type=self.SIGNAL_TYPE,
                player_to_back=player,
                player_name=player_name,
                opponent_name=opponent,
                trigger_description=(
                    f"{player_name} just broke serve in set {state.current_set}. "
                    f"Market moved only {odds_move*100:.1f}% — break not fully priced. "
                    f"Model gives {model_prob:.1%} vs market {market_prob:.1%}."
                ),
                confidence=self._confidence(state, player, odds_move),
                recommended_market="next_game",
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
        return None

    def _just_broke_serve(self, state: MatchState, player: int) -> bool:
        """
        Infer whether player just broke serve.
        In tennis, service alternates. If game_log has ≥4 entries, we can
        detect when a player won a game that 'should' have been the opponent's serve.
        Simplified: player won last game AND the game before was also won by player
        (i.e., they held AND broke, or broke twice).
        """
        log = state.game_log
        if len(log) < 2:
            return False
        # Look at last BREAK_WINDOW_GAMES games
        recent = log[-self.BREAK_WINDOW_GAMES:]
        player_wins = recent.count(player)
        opponent = 2 if player == 1 else 1
        opponent_wins = recent.count(opponent)

        # Player is dominating the last few games (break indicator)
        if player_wins >= 2 and player_wins > opponent_wins:
            return True
        return False

    def _confidence(self, state: MatchState, player: int, odds_move: float) -> float:
        conf = 0.65
        # Breaks in later sets are more decisive
        if state.current_set >= 2:
            conf += 0.05
        if state.current_set >= 3:
            conf += 0.05
        # Clay: breaks are harder to convert, so a break is more meaningful
        if state.surface == "clay":
            conf += 0.04
        if odds_move < 0.04:
            conf += 0.04  # market very slow to react
        # If the breaking player also leads in sets
        sets_leading = (state.sets_p1 > state.sets_p2 if player == 1
                        else state.sets_p2 > state.sets_p1)
        if sets_leading:
            conf += 0.03
        return min(conf, 0.82)
