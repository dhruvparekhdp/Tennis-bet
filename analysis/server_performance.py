from analysis.match_state import MatchState, ServeStats
from analysis.signal import Signal, compute_fair_odds, compute_stake


class ServerPerformanceAnalyzer:
    """
    Signal: server's 1st serve % has collapsed (<50%) for 2+ consecutive service games
    AND they have ≥2 double faults in that period. Back the returner.
    """

    SIGNAL_TYPE = "serve_degradation"
    FIRST_SERVE_THRESHOLD = 0.50
    MIN_DOUBLE_FAULTS = 2
    MIN_SERVICE_GAMES = 2           # need at least 2 data points

    def analyze(self, state: MatchState) -> Signal | None:
        if state.current_server == 0:
            return None

        server = state.current_server
        server_stats: ServeStats = state.serve_stats_p1 if server == 1 else state.serve_stats_p2

        if server_stats.service_games_played < self.MIN_SERVICE_GAMES:
            return None

        recent_pct = server_stats.recent_first_serve_pct
        recent_dfs = server_stats.recent_double_faults

        if recent_pct >= self.FIRST_SERVE_THRESHOLD:
            return None
        if recent_dfs < self.MIN_DOUBLE_FAULTS:
            return None

        # The player to back is the returner (opponent of the server)
        returner = 2 if server == 1 else 1
        server_name = state.player1_name if server == 1 else state.player2_name
        returner_name = state.player1_name if returner == 1 else state.player2_name
        returner_odds = state.odds_p1 if returner == 1 else state.odds_p2

        if returner_odds <= 1.01:
            return None

        confidence = self._confidence(recent_pct, recent_dfs, state)

        # Fair odds: give the returner a 10-15% boost in implied probability
        serve_penalty = (self.FIRST_SERVE_THRESHOLD - recent_pct) * 0.5
        base_implied = 1.0 / returner_odds
        fair_implied = min(0.95, base_implied + serve_penalty)
        fair_odds = compute_fair_odds(fair_implied)
        edge = (returner_odds - fair_odds) / fair_odds
        if edge <= 0:
            return None

        score = (f"{state.sets_p1}-{state.sets_p2} sets, "
                 f"{state.games_in_set_p1}-{state.games_in_set_p2} in set {state.current_set}")

        return Signal(
            match_id=state.match_id,
            signal_type=self.SIGNAL_TYPE,
            player_to_back=returner,
            player_name=returner_name,
            opponent_name=server_name,
            trigger_description=(
                f"{server_name} serving poorly: {recent_pct*100:.0f}% 1st serve "
                f"(last 2 games), {recent_dfs} double faults. Back the returner."
            ),
            confidence=confidence,
            recommended_market="next_game",
            current_odds=returner_odds,
            fair_odds=fair_odds,
            edge_pct=edge,
            stake_pct=compute_stake(edge, returner_odds),
            tournament=state.tournament,
            surface=state.surface,
            score_summary=score,
            match_duration_mins=state.match_duration_mins,
            timestamp=state.timestamp,
        )

    def _confidence(self, first_serve_pct: float, double_faults: int, state: MatchState) -> float:
        conf = 0.58
        if first_serve_pct < 0.40:
            conf += 0.07
        if double_faults >= 3:
            conf += 0.05
        if state.surface == "grass":    # serving matters most on grass
            conf += 0.07
        if state.surface == "clay":     # serving matters least on clay
            conf -= 0.05
        return min(conf, 0.78)
