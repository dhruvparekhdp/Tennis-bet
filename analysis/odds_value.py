from analysis.match_state import MatchState
from analysis.signal import Signal, compute_fair_odds, compute_stake


class OddsValueAnalyzer:
    """
    Signal: odds moved >25% in under 5 minutes with no games scored.
    Market overreaction — fade the move by backing the drifter.
    """

    SIGNAL_TYPE = "odds_value"
    MOVE_THRESHOLD = 0.25           # 25% odds movement
    WINDOW_MINUTES = 5
    MIN_MATCH_DURATION = 20         # ignore early-match volatility

    def analyze(self, state: MatchState) -> Signal | None:
        if state.match_duration_mins < self.MIN_MATCH_DURATION:
            return None
        if state.odds_p1 <= 1.01 or state.odds_p2 <= 1.01:
            return None

        for player in (1, 2):
            move = state.odds_change_pct_last_n_minutes(player, self.WINDOW_MINUTES)
            # Negative move = odds drifted out (player became longer)
            # We want to back a player whose odds drifted out significantly (market overreacted)
            if move > -self.MOVE_THRESHOLD:
                continue

            # Verify no games were won during this period
            g1, g2 = state.games_won_last_n_minutes(self.WINDOW_MINUTES)
            games_won_by_player = g1 if player == 1 else g2
            if games_won_by_player > 0:
                continue            # legitimate odds move (player lost games)

            current_odds = state.odds_p1 if player == 1 else state.odds_p2

            # Check if odds are already partially reverting (good sign)
            partial_revert = state.odds_change_pct_last_n_minutes(player, minutes=2)
            reverting = partial_revert > 0.03

            confidence = self._confidence(move, reverting)
            if confidence < 0.55:
                continue

            # Fair odds: implied prob before the move
            old_points = [p for p in state.odds_history if True]  # get oldest in window
            if not old_points:
                continue
            pre_move_odds = old_points[0].odds_p1 if player == 1 else old_points[0].odds_p2
            if pre_move_odds <= 1.01:
                continue

            fair_implied = 1.0 / pre_move_odds
            fair_odds = compute_fair_odds(fair_implied)
            edge = (current_odds - fair_odds) / fair_odds
            if edge <= 0:
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
                    f"Odds on {player_name} moved {abs(move)*100:.1f}% in {self.WINDOW_MINUTES} mins "
                    f"with no games scored — apparent market overreaction. "
                    f"{'Odds already reverting.' if reverting else ''}"
                ),
                confidence=confidence,
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
        return None

    def _confidence(self, move: float, reverting: bool) -> float:
        conf = 0.60
        if abs(move) > 0.35:
            conf += 0.05
        if reverting:
            conf += 0.10
        return min(conf, 0.78)
