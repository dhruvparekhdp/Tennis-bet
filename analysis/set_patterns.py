from analysis.match_state import MatchState
from analysis.signal import Signal, compute_fair_odds, compute_stake
from storage.models import PlayerStats


class SetPatternAnalyzer:
    """
    Signal: player with historical pattern of losing set 1 but winning the match
    has just lost set 1, and their odds have drifted out too far.
    """

    SIGNAL_TYPE = "set_pattern"
    # Thresholds: must have lost 1st set >60% of time but won match >65% of the time
    MIN_FIRST_SET_LOSS_RATE = 0.60
    MIN_MATCH_WIN_RATE = 0.65
    MIN_MATCHES_FOR_STATS = 15      # need enough historical data
    # Minimum drift threshold: odds must have moved at least 20% since match start
    MIN_ODDS_DRIFT = 0.20

    def analyze(self, state: MatchState, player_stats: dict[str, PlayerStats | None]) -> Signal | None:
        # Only trigger after exactly 1 set has been played (one player just lost set 1)
        if state.sets_p1 + state.sets_p2 != 1:
            return None

        # The loser of set 1 is the candidate
        if state.sets_p1 == 0:
            candidate = 1   # player1 lost set 1
            candidate_name = state.player1_name
            opponent_name = state.player2_name
            candidate_odds = state.odds_p1
        else:
            candidate = 2
            candidate_name = state.player2_name
            opponent_name = state.player1_name
            candidate_odds = state.odds_p2

        if candidate_odds <= 1.01:
            return None

        stats = player_stats.get(candidate_name)
        if stats is None or stats.matches_played < self.MIN_MATCHES_FOR_STATS:
            return None

        first_set_loss_rate = stats.first_set_losses / max(stats.matches_played, 1)
        if stats.first_set_losses == 0:
            return None
        match_win_after_set1_loss = stats.first_set_loss_wins / max(stats.first_set_losses, 1)

        if first_set_loss_rate < self.MIN_FIRST_SET_LOSS_RATE:
            return None
        if match_win_after_set1_loss < self.MIN_MATCH_WIN_RATE:
            return None

        # Odds must have drifted (player's odds went out since match start)
        odds_drift = state.odds_change_pct_last_n_minutes(candidate, minutes=120)
        if odds_drift > -self.MIN_ODDS_DRIFT:   # negative = odds drifted out
            return None

        confidence = self._confidence(first_set_loss_rate, match_win_after_set1_loss, odds_drift)

        fair_implied = match_win_after_set1_loss * 0.90  # slight haircut for uncertainty
        fair_odds = compute_fair_odds(fair_implied)
        edge = (candidate_odds - fair_odds) / fair_odds
        if edge <= 0:
            return None

        score = f"Sets: {state.sets_p1}-{state.sets_p2}"

        return Signal(
            match_id=state.match_id,
            signal_type=self.SIGNAL_TYPE,
            player_to_back=candidate,
            player_name=candidate_name,
            opponent_name=opponent_name,
            trigger_description=(
                f"{candidate_name} lost set 1 but historically wins "
                f"{match_win_after_set1_loss*100:.0f}% of matches after losing first set "
                f"(on {state.surface}). Odds drifted {abs(odds_drift)*100:.0f}% — value opportunity."
            ),
            confidence=confidence,
            recommended_market="match_winner",
            current_odds=candidate_odds,
            fair_odds=fair_odds,
            edge_pct=edge,
            stake_pct=compute_stake(edge, candidate_odds),
            tournament=state.tournament,
            surface=state.surface,
            score_summary=score,
            match_duration_mins=state.match_duration_mins,
            timestamp=state.timestamp,
        )

    def _confidence(self, set_loss_rate: float, win_after_loss_rate: float, drift: float) -> float:
        conf = 0.62
        if win_after_loss_rate > 0.72:
            conf += 0.05
        if set_loss_rate > 0.70:
            conf += 0.05
        if abs(drift) > 0.35:
            conf += 0.05
        return min(conf, 0.78)
