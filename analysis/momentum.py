from analysis.match_state import MatchState
from analysis.signal import Signal, compute_fair_odds, compute_stake
from analysis.win_probability import compute_win_probability


class MomentumAnalyzer:
    """
    Signal: player won 4+ consecutive games and odds haven't reflected it.

    Research (Gauriot & Page) shows momentum in tennis is real but modest (~7%
    per point increase). We require 4 games (not 3) and confirm via the Markov
    model to avoid false signals on noise.
    """

    SIGNAL_TYPE = "momentum"
    MIN_STREAK = 4              # raised from 3 — research shows 3-game streaks are noise
    MIN_MATCH_GAMES = 6
    # Only signal when market barely moved despite the streak (< 8%, not 15%)
    MAX_ODDS_MOVE = 0.08
    MIN_SIGNAL_ODDS = 1.30      # never signal on near-certainties

    def analyze(self, state: MatchState) -> Signal | None:
        if state.odds_p1 < self.MIN_SIGNAL_ODDS or state.odds_p2 < self.MIN_SIGNAL_ODDS:
            return None

        for player in (1, 2):
            streak = state.consecutive_games_won_by(player)
            if streak < self.MIN_STREAK:
                continue
            if state.total_games_played() < self.MIN_MATCH_GAMES:
                continue
            if state.is_tiebreak:
                continue

            odds_move = state.odds_change_pct_last_n_minutes(player, minutes=15)
            if odds_move >= self.MAX_ODDS_MOVE:
                continue  # market already adjusted

            current_odds = state.odds_p1 if player == 1 else state.odds_p2
            if current_odds <= 1.01:
                continue

            # Confirm with Markov model: model win prob must exceed market implied prob
            model_p1, model_p2 = compute_win_probability(state)
            model_prob = model_p1 if player == 1 else model_p2
            market_prob = 1.0 / current_odds
            if model_prob <= market_prob:
                continue  # model doesn't confirm edge

            # Fair odds: market-calibrated Markov probability + small momentum premium
            # Research: momentum effect ≈ 7% per point; 4-game streak → small but real premium
            momentum_premium = min(streak * 0.02, 0.08)  # capped at 8%
            fair_implied = min(0.93, model_prob + momentum_premium)
            fair_odds = compute_fair_odds(fair_implied)
            edge = (current_odds - fair_odds) / fair_odds

            if edge <= 0.05:  # require meaningful edge, not just a sliver
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
                    f"Market odds moved only {odds_move*100:.1f}% — Markov model confirms "
                    f"{model_prob:.1%} win prob vs market implied {market_prob:.1%}."
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
        conf = 0.58
        if streak >= 5:
            conf += 0.07
        if streak >= 6:
            conf += 0.05
        if odds_move < 0.03:
            conf += 0.05
        if state.surface == "clay":     # momentum persists longer on clay
            conf += 0.05
        if state.sets_p1 > state.sets_p2 if player == 1 else state.sets_p2 > state.sets_p1:
            conf += 0.04
        return min(conf, 0.80)
