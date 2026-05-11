"""
Second-set fade signal.

After a tight first set (tiebreak or 7-5), the loser of that set often
"fades" — their market odds overcorrect. The winner's market price
compresses too far, creating value on the set-loser at the start of set 2.

Research: Klaassen & Magnus (2014) found that psychological momentum from
a tiebreak is real but short-lived (~2 games). If odds haven't reverted by
game 2 of set 2, there is likely edge on the first-set loser.
"""
from __future__ import annotations

from analysis.match_state import MatchState
from analysis.signal import Signal, compute_fair_odds, compute_stake
from analysis.win_probability import compute_win_probability


class SecondSetFadeAnalyzer:
    """
    Fires at the start of set 2 when:
    - Set 1 was tight (tiebreak or 7-5)
    - The first-set winner's market odds are too short relative to model
    - The first-set loser has value

    Logic: after a tiebreak the market over-prices the set-winner by ~4-6%.
    We back the underdog (first-set loser) when model says they're closer
    to 50/50 than the market implies.
    """

    SIGNAL_TYPE = "second_set_fade"
    MIN_SIGNAL_ODDS = 1.30        # Don't fade unless loser offers real value
    MAX_SIGNAL_ODDS = 3.50        # Skip heavy underdogs — model noise is too large
    MIN_EDGE = 0.04
    # Only fire in first 2 games of set 2 (before odds normalise)
    MAX_GAMES_INTO_SET2 = 2

    def analyze(self, state: MatchState) -> Signal | None:
        # Only fires at start of set 2
        if state.current_set != 2:
            return None
        if state.is_tiebreak:
            return None
        games_played_in_set2 = state.games_in_set_p1 + state.games_in_set_p2
        if games_played_in_set2 > self.MAX_GAMES_INTO_SET2:
            return None

        # One player must have won set 1 cleanly (0-6 etc. gaps won't trigger)
        if state.sets_p1 == state.sets_p2:
            return None

        set1_winner = 1 if state.sets_p1 > state.sets_p2 else 2
        set1_loser = 2 if set1_winner == 1 else 1

        # Was set 1 tight? (tiebreak or 7-5)
        if not self._set1_was_tight(state):
            return None

        # The loser must have value
        loser_odds = state.odds_p2 if set1_loser == 2 else state.odds_p1
        if loser_odds < self.MIN_SIGNAL_ODDS or loser_odds > self.MAX_SIGNAL_ODDS:
            return None

        # Markov check — model should give loser closer to fair odds
        model_p1, model_p2 = compute_win_probability(state)
        model_prob = model_p1 if set1_loser == 1 else model_p2
        market_prob = 1.0 / loser_odds

        edge = model_prob - market_prob
        if edge < self.MIN_EDGE:
            return None

        fair_odds = compute_fair_odds(model_prob)
        edge_pct = (loser_odds - fair_odds) / fair_odds

        loser_name = state.player1_name if set1_loser == 1 else state.player2_name
        winner_name = state.player2_name if set1_loser == 1 else state.player1_name
        score = f"Sets {state.sets_p1}-{state.sets_p2}, {state.games_in_set_p1}-{state.games_in_set_p2} in set 2"

        return Signal(
            match_id=state.match_id,
            signal_type=self.SIGNAL_TYPE,
            player_to_back=set1_loser,
            player_name=loser_name,
            opponent_name=winner_name,
            trigger_description=(
                f"{loser_name} lost a tight set 1 — market over-reacted. "
                f"Model gives {model_prob:.1%} vs market {market_prob:.1%} "
                f"(edge {edge:.1%}). Fading the set-winner at {state.current_set=}."
            ),
            confidence=self._confidence(state, edge),
            recommended_market="set_winner_set2",
            current_odds=loser_odds,
            fair_odds=fair_odds,
            edge_pct=edge_pct,
            stake_pct=compute_stake(edge_pct, loser_odds),
            tournament=state.tournament,
            surface=state.surface,
            score_summary=score,
            match_duration_mins=state.match_duration_mins,
            timestamp=state.timestamp,
        )

    def _set1_was_tight(self, state: MatchState) -> bool:
        """
        Infer whether set 1 was tight from the game_log or total games.
        A tiebreak set is 7-6 (13 games); a 7-5 set is 12 games.
        We can check the game_log length — if ≥12 games were played before
        the set ended, it was close.
        """
        # If we have game_log, count set-1 games
        log = state.game_log
        if len(log) >= 12:
            # First 12+ games played → set 1 was at least 6-6 or 7-5
            return True
        # Fallback: if total games in match are already ≥12 and we're in set 2
        # at game 0-2, then set 1 had ≥10 games
        total = state.total_games_played()
        games_in_set2 = state.games_in_set_p1 + state.games_in_set_p2
        set1_games = total - games_in_set2
        return set1_games >= 12

    def _confidence(self, state: MatchState, edge: float) -> float:
        conf = 0.65
        # Bigger edge → more confident
        if edge > 0.08:
            conf += 0.04
        if edge > 0.12:
            conf += 0.03
        # Clay: second-set comebacks more common
        if state.surface == "clay":
            conf += 0.03
        # Very early in set 2 → market hasn't had time to correct
        if state.games_in_set_p1 + state.games_in_set_p2 == 0:
            conf += 0.04
        return min(conf, 0.80)
