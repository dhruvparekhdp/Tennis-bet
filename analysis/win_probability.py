"""
Analytical Markov-chain win probability model for tennis.

No training data required — computes fair win probabilities from
first principles using the current match state (score + serve stats).
"""
from __future__ import annotations

from functools import lru_cache

from analysis.match_state import MatchState

# Surface defaults for probability server wins a point
_SURFACE_P_SERVE: dict[str, float] = {
    "clay": 0.62,
    "grass": 0.70,
    "hard": 0.65,
    "indoor_hard": 0.65,
}
_DEFAULT_P_SERVE = 0.65


# ── Point / game level ────────────────────────────────────────────────────────


@lru_cache(maxsize=512)
def _prob_win_game(p: float, pts_a: int = 0, pts_b: int = 0) -> float:
    """P(server wins game) starting from pts_a : pts_b.

    Uses closed-form deuce formula to avoid infinite recursion.
    ``p`` should be rounded to 3 dp for effective caching.
    """
    if pts_a >= 4 and pts_a - pts_b >= 2:
        return 1.0
    if pts_b >= 4 and pts_b - pts_a >= 2:
        return 0.0
    if pts_a >= 3 and pts_b >= 3:
        # Deuce closed form
        p2 = p * p
        return p2 / (p2 + (1 - p) ** 2)
    return p * _prob_win_game(p, pts_a + 1, pts_b) + (1 - p) * _prob_win_game(p, pts_a, pts_b + 1)


def prob_win_game(p: float) -> float:
    """P(server wins game from 0:0) given point-win probability p."""
    return _prob_win_game(round(p, 3))


# ── Set level ─────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1024)
def _prob_win_set(p_game: float, ga: int, gb: int) -> float:
    """P(player wins set) starting from games_a : games_b.

    ``p_game`` is P(player wins a game when serving/returning — averaged).
    """
    if ga >= 6 and ga - gb >= 2:
        return 1.0
    if gb >= 6 and gb - ga >= 2:
        return 0.0
    if ga == 6 and gb == 6:
        # Tiebreak: approximate as winning a single point at p_game
        return p_game
    return (
        p_game * _prob_win_set(p_game, ga + 1, gb)
        + (1 - p_game) * _prob_win_set(p_game, ga, gb + 1)
    )


def prob_win_set_from(ga: int, gb: int, p_game: float) -> float:
    return _prob_win_set(round(p_game, 3), ga, gb)


# ── Match level ───────────────────────────────────────────────────────────────


@lru_cache(maxsize=256)
def _prob_win_match(p_set: float, sa: int, sb: int, sets_needed: int) -> float:
    """P(player wins match) starting from sets_a : sets_b."""
    if sa >= sets_needed:
        return 1.0
    if sb >= sets_needed:
        return 0.0
    return (
        p_set * _prob_win_match(p_set, sa + 1, sb, sets_needed)
        + (1 - p_set) * _prob_win_match(p_set, sa, sb + 1, sets_needed)
    )


def prob_win_match_from(sa: int, sb: int, p_set: float, best_of: int = 3) -> float:
    sets_needed = (best_of + 1) // 2
    return _prob_win_match(round(p_set, 3), sa, sb, sets_needed)


# ── Public API ────────────────────────────────────────────────────────────────


def _estimate_p_serve(state: MatchState, player: int) -> float:
    """Estimate probability server wins a point for ``player`` (1 or 2)."""
    stats = state.serve_stats_p1 if player == 1 else state.serve_stats_p2
    if stats.service_games_played > 0:
        fsp = stats.first_serve_pct
        # Rough approximation: p(win point on serve) ≈ fsp * 0.70 + (1-fsp) * 0.45
        p = fsp * 0.70 + (1 - fsp) * 0.45
        return max(0.50, min(0.85, p))
    return _SURFACE_P_SERVE.get(state.surface, _DEFAULT_P_SERVE)


def _compute_p_game(state: MatchState, player: int) -> float:
    """Average game-win probability for ``player`` accounting for both
    serving and returning games."""
    p_serve = _estimate_p_serve(state, player)
    opponent = 2 if player == 1 else 1
    p_opp_serve = _estimate_p_serve(state, opponent)

    p_game_serve = prob_win_game(p_serve)
    p_game_return = 1.0 - prob_win_game(p_opp_serve)
    return (p_game_serve + p_game_return) / 2.0


def _determine_best_of(state: MatchState) -> int:
    """Infer best-of-3 vs best-of-5 from how many sets have been played."""
    total_sets = state.sets_p1 + state.sets_p2
    # Grand Slams men's use best-of-5; anything with 3+ sets played could be bo5
    if total_sets >= 3:
        return 5
    return 3


def compute_win_probability(state: MatchState) -> tuple[float, float]:
    """Return (p1_win_prob, p2_win_prob) from current match state.

    Uses Markov-chain model — no ML required.
    """
    best_of = _determine_best_of(state)

    p_game_p1 = _compute_p_game(state, 1)
    p_game_p2 = _compute_p_game(state, 2)

    # Probability each player wins the current set from current games score
    p1_set_win = prob_win_set_from(
        state.games_in_set_p1, state.games_in_set_p2, p_game_p1
    )
    p2_set_win = prob_win_set_from(
        state.games_in_set_p2, state.games_in_set_p1, p_game_p2
    )

    # Use p_set as the blended game-level probability carried forward
    p1_match_win = prob_win_match_from(state.sets_p1, state.sets_p2, p1_set_win, best_of)
    p2_match_win = prob_win_match_from(state.sets_p2, state.sets_p1, p2_set_win, best_of)

    # Normalise
    total = p1_match_win + p2_match_win
    if total <= 0:
        return 0.5, 0.5
    return p1_match_win / total, p2_match_win / total


def model_fair_odds(state: MatchState) -> tuple[float, float]:
    """Return (fair_odds_p1, fair_odds_p2) from the analytical Markov model."""
    p1, p2 = compute_win_probability(state)
    fair_p1 = round(1.0 / p1, 3) if p1 > 0 else 999.0
    fair_p2 = round(1.0 / p2, 3) if p2 > 0 else 999.0
    return fair_p1, fair_p2
