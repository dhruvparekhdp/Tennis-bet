from analysis.match_state import MatchState
from analysis.signal import Signal, compute_fair_odds, compute_stake


class OddsValueAnalyzer:
    """
    Signal: odds drifted >25% in under 5 minutes with no games scored.
    Market overreaction — fade the move by backing the drifter.
    """

    SIGNAL_TYPE = "odds_value"
    MOVE_THRESHOLD = 0.25           # 25% drift in the window
    WINDOW_MINUTES = 5
    MIN_MATCH_DURATION = 20
    MIN_SIGNAL_ODDS = 1.30          # checked per-player inside loop, not at entry

    def analyze(self, state: MatchState) -> Signal | None:
        if state.match_duration_mins < self.MIN_MATCH_DURATION:
            return None
        if state.odds_p1 <= 1.01 or state.odds_p2 <= 1.01:
            return None

        for player in (1, 2):
            move = state.odds_change_pct_last_n_minutes(player, self.WINDOW_MINUTES)
            # Negative = odds drifted out (player became longer) — we want to back them
            if move > -self.MOVE_THRESHOLD:
                continue

            current_odds = state.odds_p1 if player == 1 else state.odds_p2
            if current_odds < self.MIN_SIGNAL_ODDS:
                continue  # only check the player being backed

            # Verify no games were won by this player during the drift window
            g1, g2 = state.games_won_last_n_minutes(self.WINDOW_MINUTES)
            games_won_by_player = g1 if player == 1 else g2
            if games_won_by_player > 0:
                continue  # legitimate odds move (player lost games)

            # Pre-move odds: last snapshot BEFORE the window opened
            now = state.timestamp
            cutoff = now.timestamp() - self.WINDOW_MINUTES * 60
            pre_window = [p for p in state.odds_history if p.timestamp.timestamp() < cutoff]
            if not pre_window:
                continue
            pre_move_odds = pre_window[-1].odds_p1 if player == 1 else pre_window[-1].odds_p2
            if pre_move_odds <= 1.01:
                continue

            # Check if odds are already partially reverting (good confirming sign)
            partial_revert = state.odds_change_pct_last_n_minutes(player, minutes=2)
            reverting = partial_revert > 0.03

            confidence = self._confidence(move, reverting)
            if confidence < 0.65:
                continue

            fair_implied = 1.0 / pre_move_odds
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
                    f"Odds on {player_name} drifted {abs(move)*100:.1f}% in {self.WINDOW_MINUTES} mins "
                    f"with no games scored — market overreaction. "
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
        conf = 0.65                 # base above global threshold
        if abs(move) > 0.40:
            conf += 0.07
        elif abs(move) > 0.30:
            conf += 0.04
        if reverting:
            conf += 0.08
        return min(conf, 0.82)
