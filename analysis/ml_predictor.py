"""
In-play match winner predictor using logistic regression.

Trained incrementally on completed matches stored in DB.
Falls back to analytical Markov chain when <MIN_SAMPLES completed matches available.
"""
from __future__ import annotations

import structlog
from dataclasses import dataclass, astuple
from pathlib import Path

import numpy as np
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score
import joblib

from analysis.match_state import MatchState
from analysis.win_probability import compute_win_probability

log = structlog.get_logger()


@dataclass
class MatchFeatures:
    p1_sets_lead: int           # sets_p1 - sets_p2
    p1_games_lead: int          # games_in_set_p1 - games_in_set_p2
    current_set: int            # 1-5
    p1_momentum: int            # consecutive games positive if p1 on streak, negative if p2
    p1_serve_pct: float         # first_serve_pct from serve_stats_p1 (default 0.62)
    p2_serve_pct: float
    surface_clay: int           # one-hot
    surface_grass: int
    surface_indoor: int
    match_progress: float       # current_set / 5
    p1_opening_implied: float   # 1/opening_odds_p1 if available else 0.5

    def to_array(self) -> list[float]:
        return [float(x) for x in astuple(self)]


def extract_features(state: MatchState) -> MatchFeatures:
    """Extract MatchFeatures from a live MatchState."""
    p1_sets_lead = state.sets_p1 - state.sets_p2
    p1_games_lead = state.games_in_set_p1 - state.games_in_set_p2

    # Momentum: positive = p1 consecutive, negative = p2 consecutive
    p1_consec = state.consecutive_games_won_by(1)
    p2_consec = state.consecutive_games_won_by(2)
    if p1_consec > 0:
        p1_momentum = p1_consec
    elif p2_consec > 0:
        p1_momentum = -p2_consec
    else:
        p1_momentum = 0

    p1_serve_pct = state.serve_stats_p1.first_serve_pct if state.serve_stats_p1.service_games_played > 0 else 0.62
    p2_serve_pct = state.serve_stats_p2.first_serve_pct if state.serve_stats_p2.service_games_played > 0 else 0.62

    surface = state.surface.lower()
    surface_clay = 1 if surface == "clay" else 0
    surface_grass = 1 if surface == "grass" else 0
    surface_indoor = 1 if surface == "indoor_hard" else 0

    match_progress = state.current_set / 5.0

    # Opening implied probability from earliest odds history entry
    p1_opening_implied = 0.5
    if state.odds_history:
        first = state.odds_history[0]
        if first.odds_p1 > 1.0:
            p1_opening_implied = 1.0 / first.odds_p1
    elif state.odds_p1 > 1.0:
        p1_opening_implied = 1.0 / state.odds_p1

    return MatchFeatures(
        p1_sets_lead=p1_sets_lead,
        p1_games_lead=p1_games_lead,
        current_set=state.current_set,
        p1_momentum=p1_momentum,
        p1_serve_pct=p1_serve_pct,
        p2_serve_pct=p2_serve_pct,
        surface_clay=surface_clay,
        surface_grass=surface_grass,
        surface_indoor=surface_indoor,
        match_progress=match_progress,
        p1_opening_implied=p1_opening_implied,
    )


class MLPredictor:
    """Logistic-regression in-play win predictor with Markov-chain fallback."""

    MIN_SAMPLES = 50
    MODEL_PATH = Path("tennis_ml_model.pkl")

    def __init__(self) -> None:
        self._trained = False
        self._model: Pipeline | None = None
        self._try_load_model()

    def _try_load_model(self) -> None:
        if self.MODEL_PATH.exists():
            try:
                self._model = joblib.load(self.MODEL_PATH)
                self._trained = True
                log.info("ml_model_loaded", path=str(self.MODEL_PATH))
            except Exception as exc:
                log.warning("ml_model_load_failed", error=str(exc))

    def predict(self, state: MatchState) -> tuple[float, float]:
        """Return (p1_prob, p2_prob). Uses ML model if trained, else Markov chain."""
        if self._trained and self._model is not None:
            try:
                features = extract_features(state)
                X = np.array([features.to_array()])
                proba = self._model.predict_proba(X)[0]
                # classes_ order: 1, 2 after training
                classes = list(self._model.classes_)
                if len(proba) == 2:
                    idx1 = classes.index(1) if 1 in classes else 0
                    idx2 = classes.index(2) if 2 in classes else 1
                    return float(proba[idx1]), float(proba[idx2])
            except Exception as exc:
                log.warning("ml_predict_failed", error=str(exc))

        # Fallback to analytical Markov chain
        return compute_win_probability(state)

    def train(self, X: np.ndarray, y: np.ndarray) -> None:
        """Fit logistic regression pipeline, save to disk."""
        pipeline = Pipeline([
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(max_iter=1000)),
        ])
        pipeline.fit(X, y)

        y_pred = pipeline.predict(X)
        accuracy = float(accuracy_score(y, y_pred))

        joblib.dump(pipeline, self.MODEL_PATH)
        self._model = pipeline
        self._trained = True

        log.info("ml_model_trained", accuracy=accuracy, n_samples=len(y))

    async def maybe_retrain(self, repository: "Repository") -> None:  # noqa: F821
        """Load training samples from DB and retrain if enough samples exist."""
        try:
            X, y = await repository.get_training_data()
        except Exception as exc:
            log.error("ml_retrain_load_failed", error=str(exc))
            return

        if len(y) < self.MIN_SAMPLES:
            log.info("ml_retrain_skipped", n_samples=len(y), min_required=self.MIN_SAMPLES)
            return

        try:
            self.train(X, y)
        except Exception as exc:
            log.error("ml_retrain_failed", error=str(exc))
