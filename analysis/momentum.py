from analysis.match_state import MatchState
from analysis.signal import Signal, compute_fair_odds, compute_stake
from analysis.win_probability import compute_win_probability


class MomentumAnalyzer:
    """
    Signal: player won 3+ consecutive games and odds haven't reflected it.

    Quality gate: Markov model (calibrated from market odds) must confirm that
    the score-state gives this player higher win prob than the current market price.
    This replaces the old streak-count-only approach that generated noise.
    """

    SIGNAL_TYPE = "momentum"
    MIN_STREAK = 3
    MIN_MATCH_GAMES = 6
    # Market must have moved <12% during the streak (lagging indicator)
    MAX_ODDS_MOVE = 0.12
    MIN_SIGNAL_ODDS = 1.30      # only applies to the player being backed, checked inside loop

    def analyze(self, state: MatchState) -> Signal | None:
        for player in (1, 2):
            streak = state.consecutive_games_won_by(player)
            if streak < self.MIN_STREAK:
                continue
            if state.total_games_played() < self.MIN_MATCH_GAMES:
                continue
            if state.is_tiebreak:
                continue

            current_odds = state.odds_p1 if player == 1 else state.odds_p2
            if current_odds < self.MIN_SIGNAL_ODDS:
                continue  # don't back near-certainties

            odds_move = state.odds_change_pct_last_n_minutes(player, minutes=15)
            if odds_move >= self.MAX_ODDS_MOVE:
                continue  # market already adjusted

            # Markov confirmation: score-based model must give higher prob than market
            model_p1, model_p2 = compute_win_probability(state)
            model_prob = model_p1 if player == 1 else model_p2
            market_prob = 1.0 / current_odds
            if model_prob <= market_prob:
                continue

            # Fair odds: model probability + small momentum premium (capped at 8%)
            momentum_premium = min(streak * 0.02, 0.08)
            fair_implied = min(0.93, model_prob + momentum_premium)
            fair_odds = compute_fair_odds(fair_implied)
            edge = (current_odds - fair_odds) / fair_odds
            if edge <= 0.03:
                continue

            opponent = state.player2_name if player == 1 else state.player1_name
            player_name = state.player1_name if player == 1 else state.player2_name
            score = (f"{state.sets_p1}-{state.sets_p2} sets, "
                     f"{state.games_in_set_p1}-{state.games_in_set_p2} in set {state.current_set}")

            return Signal(
                match_id=state.match_id,
                signal_type=self.SIGNAL_TYPE,
                player_to_back=player,
                player_name=player_name,
                opponent_name=opponent,
                trigger_description=(
                    f"{player_name} won last {streak} consecutive games. "
                    f"Market moved only {odds_move*100:.1f}% — Markov model gives "
                    f"{model_prob:.1%} vs market implied {market_prob:.1%}."
                ),
                confidence=self._confidence(streak, odds_move, state, player),
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

    def _confidence(self, streak: int, odds_move: float, state: MatchState, player: int) -> float:
        conf = 0.65                 # base above global threshold so 3-game streaks can fire
        if streak >= 4:
            conf += 0.05
        if streak >= 5:
            conf += 0.05
        if odds_move < 0.04:
            conf += 0.04
        if state.surface == "clay":
            conf += 0.04
        if state.sets_p1 > state.sets_p2 if player == 1 else state.sets_p2 > state.sets_p1:
            conf += 0.03
        return min(conf, 0.82)
