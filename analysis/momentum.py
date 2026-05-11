from analysis.match_state import MatchState
from analysis.signal import Signal, compute_fair_odds, compute_stake


class MomentumAnalyzer:
    """
    Signal: player won 3+ consecutive games but odds haven't reflected it.
    Bet: back the momentum player on the next game market.
    """

    SIGNAL_TYPE = "momentum"
    MIN_STREAK = 3
    MIN_MATCH_GAMES = 4             # avoid signalling on the first few games

    def analyze(self, state: MatchState) -> Signal | None:
        for player in (1, 2):
            streak = state.consecutive_games_won_by(player)
            if streak < self.MIN_STREAK:
                continue
            if state.total_games_played() < self.MIN_MATCH_GAMES:
                continue
            if state.is_tiebreak:
                continue

            # How much have odds moved during this streak?
            odds_move = state.odds_change_pct_last_n_minutes(player, minutes=15)

            # Market is lagging if odds shortened less than 15%
            if odds_move >= 0.15:
                continue

            confidence = self._confidence(streak, odds_move, state, player)
            current_odds = state.odds_p1 if player == 1 else state.odds_p2
            if current_odds <= 1.01:
                continue

            # Fair odds: current odds shortened by the momentum premium
            momentum_premium = streak * 0.06
            fair_implied = min(0.95, (1 / current_odds) + momentum_premium)
            fair_odds = compute_fair_odds(fair_implied)
            edge = (current_odds - fair_odds) / fair_odds

            if edge <= 0:
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
                    f"Odds moved only {odds_move*100:.1f}% — market lagging momentum."
                ),
                confidence=confidence,
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
        conf = 0.55
        if streak >= 4:
            conf += 0.10
        if streak >= 5:
            conf += 0.05
        if odds_move < 0.05:            # market barely moved
            conf += 0.05
        if state.surface == "clay":     # momentum persists longer on clay
            conf += 0.05
        if state.sets_p1 > state.sets_p2 if player == 1 else state.sets_p2 > state.sets_p1:
            conf += 0.05                # momentum player already leading in sets
        return min(conf, 0.82)
