from analysis.match_state import MatchState
from analysis.signal import Signal, compute_fair_odds, compute_stake


class FatigueAnalyzer:
    """
    Signal: match >2.5 hours, >24 total games, and the serving player's
    1st serve % has dropped >15 percentage points from their season average.
    Back the fresher-looking player.
    """

    SIGNAL_TYPE = "fatigue"
    MIN_DURATION_MINS = 150         # 2.5 hours
    MIN_TOTAL_GAMES = 24
    SERVE_DROP_THRESHOLD = 0.15     # 15pp drop from average
    BASELINE_FIRST_SERVE_PCT = 0.62 # ATP/WTA average when no historical data

    def analyze(self, state: MatchState) -> Signal | None:
        if state.match_duration_mins < self.MIN_DURATION_MINS:
            return None
        if state.total_games_played() < self.MIN_TOTAL_GAMES:
            return None

        # Evaluate both players for fatigue indicators; pick the one showing more
        fatigue_score = {}
        for player in (1, 2):
            stats = state.serve_stats_p1 if player == 1 else state.serve_stats_p2
            baseline = self.BASELINE_FIRST_SERVE_PCT
            if stats.service_games_played < 4:
                continue
            # Approximate early-match serve pct from first 2 service games
            if len(stats.first_serve_pct_history) >= 2:
                early_pct = sum(stats.first_serve_pct_history[:2]) / 2
            else:
                early_pct = baseline
            drop = early_pct - stats.first_serve_pct
            if drop >= self.SERVE_DROP_THRESHOLD:
                fatigue_score[player] = drop

        if not fatigue_score:
            return None

        # The fatigued player is the one with biggest serve drop
        tired_player = max(fatigue_score, key=lambda p: fatigue_score[p])
        fresher_player = 2 if tired_player == 1 else 1
        serve_drop = fatigue_score[tired_player]

        tired_name = state.player1_name if tired_player == 1 else state.player2_name
        fresher_name = state.player1_name if fresher_player == 1 else state.player2_name
        fresher_odds = state.odds_p1 if fresher_player == 1 else state.odds_p2

        if fresher_odds <= 1.01:
            return None

        # Don't signal if both players are similarly fatigued
        other_drop = fatigue_score.get(fresher_player, 0.0)
        if other_drop > 0.10:       # fresher player also showing fatigue
            return None

        confidence = self._confidence(serve_drop, state, tired_player)

        # Fair odds: fatigue premium on top of current implied prob
        fatigue_premium = serve_drop * 0.4
        base_implied = 1.0 / fresher_odds
        fair_implied = min(0.95, base_implied + fatigue_premium)
        fair_odds = compute_fair_odds(fair_implied)
        edge = (fresher_odds - fair_odds) / fair_odds
        if edge <= 0:
            return None

        score = (f"{state.sets_p1}-{state.sets_p2} sets, "
                 f"{state.games_in_set_p1}-{state.games_in_set_p2} in set {state.current_set}")

        tired_stats = state.serve_stats_p1 if tired_player == 1 else state.serve_stats_p2

        return Signal(
            match_id=state.match_id,
            signal_type=self.SIGNAL_TYPE,
            player_to_back=fresher_player,
            player_name=fresher_name,
            opponent_name=tired_name,
            trigger_description=(
                f"{tired_name} showing fatigue: 1st serve dropped {serve_drop*100:.0f}pp "
                f"from match start ({tired_stats.first_serve_pct*100:.0f}% now). "
                f"Match running {state.match_duration_mins} mins, {state.total_games_played()} games played."
            ),
            confidence=confidence,
            recommended_market="next_set",
            current_odds=fresher_odds,
            fair_odds=fair_odds,
            edge_pct=edge,
            stake_pct=compute_stake(edge, fresher_odds),
            tournament=state.tournament,
            surface=state.surface,
            score_summary=score,
            match_duration_mins=state.match_duration_mins,
            timestamp=state.timestamp,
        )

    def _confidence(self, serve_drop: float, state: MatchState, tired_player: int) -> float:
        conf = 0.55
        if serve_drop > 0.20:
            conf += 0.07
        if state.match_duration_mins > 200:
            conf += 0.05
        if state.surface == "clay":     # clay is more physically demanding
            conf += 0.05
        # Penalise if tired player is the known favourite (they might just be pacing)
        tired_odds = state.odds_p1 if tired_player == 1 else state.odds_p2
        if tired_odds < 1.50:
            conf -= 0.10
        return max(0.50, min(conf, 0.72))
