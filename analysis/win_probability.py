"""
Analytical Markov-chain win probability model for tennis.

Key improvement: when live serve stats are unavailable, the model calibrates
its serve probabilities to match market-implied odds at 0-0 score, rather than
using flat surface defaults. This anchors the prior to market efficiency
(Pinnacle line accuracy ~74%) and only applies score-based Markov adjustments
on top of that baseline.

Reference: Klaassen & Magnus (2003) — point iid approximation is acceptable
structurally, but the input p(server wins point) must reflect player quality.
"""
from __future__ import annotations

from functools import lru_cache

from analysis.match_state import MatchState

# Surface defaults — only used when BOTH odds AND serve stats are unavailable
_SURFACE_P_SERVE: dict[str, float] = {
    "clay": 0.62,
    "grass": 0.70,
    "hard": 0.65,
    "indoor_hard": 0.65,
}
_DEFAULT_P_SERVE = 0.65

# Grand Slam tournaments use best-of-5 (men's singles)
_GRAND_SLAMS = frozenset({
    "australian open", "roland garros", "french open",
    "wimbledon", "us open",
})


# ── Point / game level ────────────────────────────────────────────────────────

@lru_cache(maxsize=512)
def _prob_win_game(p: float, pts_a: int = 0, pts_b: int = 0) -> float:
    """P(server wins game) starting from pts_a : pts_b."""
    if pts_a >= 4 and pts_a - pts_b >= 2:
        return 1.0
    if pts_b >= 4 and pts_b - pts_a >= 2:
        return 0.0
    if pts_a >= 3 and pts_b >= 3:
        p2 = p * p
        return p2 / (p2 + (1 - p) ** 2)
    return p * _prob_win_game(p, pts_a + 1, pts_b) + (1 - p) * _prob_win_game(p, pts_a, pts_b + 1)


def prob_win_game(p: float) -> float:
    return _prob_win_game(round(p, 3))


# ── Set level ─────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1024)
def _prob_win_set(p_game: float, ga: int, gb: int) -> float:
    if ga >= 6 and ga - gb >= 2:
        return 1.0
    if gb >= 6 and gb - ga >= 2:
        return 0.0
    if ga == 6 and gb == 6:
        return p_game  # tiebreak approximation
    return (
        p_game * _prob_win_set(p_game, ga + 1, gb)
        + (1 - p_game) * _prob_win_set(p_game, ga, gb + 1)
    )


def prob_win_set_from(ga: int, gb: int, p_game: float) -> float:
    return _prob_win_set(round(p_game, 3), ga, gb)


# ── Match level ───────────────────────────────────────────────────────────────

@lru_cache(maxsize=256)
def _prob_win_match(p_set: float, sa: int, sb: int, sets_needed: int) -> float:
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


# ── Market calibration ────────────────────────────────────────────────────────

def _calibrate_p_game_from_market(target_match_win_prob: float, best_of: int = 3) -> float:
    """
    Binary-search for p_game such that prob_win_match at 0-0 ≈ target_match_win_prob.

    This converts a market-implied win probability into a per-game win probability
    that the Markov chain can update as the score changes. Allows the model to
    start from a market-calibrated baseline rather than surface averages.
    """
    sets_needed = (best_of + 1) // 2
    # Clamp target to a solvable range (markets never price a player as certain)
    target = max(0.05, min(0.95, target_match_win_prob))
    lo, hi = 0.01, 0.99
    for _ in range(40):
        mid = (lo + hi) / 2.0
        p_set = _prob_win_set(round(mid, 3), 0, 0)
        p_match = _prob_win_match(round(p_set, 3), 0, 0, sets_needed)
        if p_match < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


# ── Internal helpers ─────────────────────────────────────────────────────────

def _estimate_p_serve(state: MatchState, player: int) -> float:
    """Estimate p(server wins point) from live serve stats, falling back to surface default."""
    stats = state.serve_stats_p1 if player == 1 else state.serve_stats_p2
    if stats.service_games_played > 0:
        fsp = stats.first_serve_pct
        # p(win point on serve) ≈ fsp * 0.70 + (1-fsp) * 0.45
        p = fsp * 0.70 + (1 - fsp) * 0.45
        return max(0.50, min(0.85, p))
    return _SURFACE_P_SERVE.get(state.surface, _DEFAULT_P_SERVE)


def _compute_p_game(state: MatchState, player: int) -> float:
    """Average game-win probability for player accounting for serving and returning."""
    p_serve = _estimate_p_serve(state, player)
    opponent = 2 if player == 1 else 1
    p_opp_serve = _estimate_p_serve(state, opponent)
    p_game_serve = prob_win_game(p_serve)
    p_game_return = 1.0 - prob_win_game(p_opp_serve)
    return (p_game_serve + p_game_return) / 2.0


def _determine_best_of(state: MatchState) -> int:
    """Infer best-of-3 vs best-of-5. Grand Slam men's singles use best-of-5."""
    tournament_lower = state.tournament.lower()
    for slam in _GRAND_SLAMS:
        if slam in tournament_lower:
            return 5
    # Fallback: if we've already seen 3+ sets, must be best-of-5
    if state.sets_p1 + state.sets_p2 >= 3:
        return 5
    return 3


# ── Public API ────────────────────────────────────────────────────────────────

def compute_win_probability(state: MatchState) -> tuple[float, float]:
    """Return (p1_win_prob, p2_win_prob) from current match state.

    When live serve stats are unavailable, calibrates the Markov model's
    baseline to market-implied odds instead of surface defaults. This makes
    the model's starting point consistent with market efficiency and only
    applies score-based adjustments on top.
    """
    best_of = _determine_best_of(state)

    no_serve_stats = (
        state.serve_stats_p1.service_games_played == 0
        and state.serve_stats_p2.service_games_played == 0
    )
    has_market_odds = state.odds_p1 > 1.01 and state.odds_p2 > 1.01

    if no_serve_stats and has_market_odds:
        # Normalize market implied probs (remove bookmaker margin)
        raw_p1 = 1.0 / state.odds_p1
        raw_p2 = 1.0 / state.odds_p2
        total = raw_p1 + raw_p2
        market_p1 = raw_p1 / total
        market_p2 = raw_p2 / total

        p_game_p1 = _calibrate_p_game_from_market(market_p1, best_of)
        p_game_p2 = _calibrate_p_game_from_market(market_p2, best_of)
    else:
        p_game_p1 = _compute_p_game(state, 1)
        p_game_p2 = _compute_p_game(state, 2)

    p1_set_win = prob_win_set_from(state.games_in_set_p1, state.games_in_set_p2, p_game_p1)
    p2_set_win = prob_win_set_from(state.games_in_set_p2, state.games_in_set_p1, p_game_p2)

    p1_match_win = prob_win_match_from(state.sets_p1, state.sets_p2, p1_set_win, best_of)
    p2_match_win = prob_win_match_from(state.sets_p2, state.sets_p1, p2_set_win, best_of)

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
