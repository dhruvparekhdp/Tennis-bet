from __future__ import annotations

import json
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from storage.models import (
    Match, MatchCompletion, MatchResult, MatchSnapshot,
    OddsSnapshot, PlayerStats, SignalLog,
)


class Repository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ── Match ──────────────────────────────────────────────────────────────

    async def upsert_match(self, match_id: str, player1: str, player2: str,
                           tournament: str, surface: str) -> None:
        result = await self.session.get(Match, match_id)
        if result is None:
            self.session.add(Match(
                id=match_id, player1=player1, player2=player2,
                tournament=tournament, surface=surface,
            ))
        else:
            result.last_updated = datetime.utcnow()
        await self.session.commit()

    async def mark_match_finished(self, match_id: str) -> None:
        match = await self.session.get(Match, match_id)
        if match:
            match.is_finished = True
            match.last_updated = datetime.utcnow()
            await self.session.commit()

    # ── OddsSnapshot ───────────────────────────────────────────────────────

    async def save_odds_snapshot(self, match_id: str, odds_p1: float, odds_p2: float) -> None:
        self.session.add(OddsSnapshot(
            match_id=match_id, odds_p1=odds_p1, odds_p2=odds_p2,
            timestamp=datetime.utcnow(),
        ))
        await self.session.commit()

    async def get_recent_odds(self, match_id: str, minutes: int = 10) -> list[OddsSnapshot]:
        since = datetime.utcnow() - timedelta(minutes=minutes)
        result = await self.session.execute(
            select(OddsSnapshot)
            .where(OddsSnapshot.match_id == match_id)
            .where(OddsSnapshot.timestamp >= since)
            .order_by(OddsSnapshot.timestamp)
        )
        return list(result.scalars())

    # ── SignalLog ──────────────────────────────────────────────────────────

    async def log_signal(
        self, match_id: str, signal_type: str, player_to_back: int,
        trigger_description: str, confidence: float, recommended_market: str,
        current_odds: float, fair_odds: float, edge_pct: float, stake_pct: float,
        player_name: str = "", opponent_name: str = "",
        tournament: str = "", surface: str = "hard",
        model_win_prob: float = 0.0, score_at_signal: str = "",
        sets_p1: int = 0, sets_p2: int = 0,
        games_p1: int = 0, games_p2: int = 0,
    ) -> None:
        self.session.add(SignalLog(
            match_id=match_id, signal_type=signal_type, player_to_back=player_to_back,
            player_name=player_name, opponent_name=opponent_name,
            tournament=tournament, surface=surface,
            trigger_description=trigger_description, confidence=confidence,
            recommended_market=recommended_market, current_odds=current_odds,
            fair_odds=fair_odds, edge_pct=edge_pct, stake_pct=stake_pct,
            model_win_prob=model_win_prob, score_at_signal=score_at_signal,
            sets_p1_at_signal=sets_p1, sets_p2_at_signal=sets_p2,
            games_p1_at_signal=games_p1, games_p2_at_signal=games_p2,
            outcome="pending", match_winner=0,
            timestamp=datetime.utcnow(),
        ))
        await self.session.commit()

    async def update_signal_outcomes(self, match_id: str, winner: int) -> tuple[int, int]:
        """
        Mark all pending signals for a match as won or lost.
        Returns (total_signals, correct_signals).
        """
        result = await self.session.execute(
            select(SignalLog)
            .where(SignalLog.match_id == match_id)
            .where(SignalLog.outcome == "pending")
        )
        signals = list(result.scalars())
        correct = 0
        for sig in signals:
            sig.match_winner = winner
            sig.outcome = "won" if sig.player_to_back == winner else "lost"
            if sig.outcome == "won":
                correct += 1
        await self.session.commit()
        return len(signals), correct

    async def get_last_signal_time(self, match_id: str, signal_type: str) -> datetime | None:
        result = await self.session.execute(
            select(SignalLog.timestamp)
            .where(SignalLog.match_id == match_id)
            .where(SignalLog.signal_type == signal_type)
            .order_by(SignalLog.timestamp.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_recent_signals(self, hours: int = 24) -> list[SignalLog]:
        since = datetime.utcnow() - timedelta(hours=hours)
        result = await self.session.execute(
            select(SignalLog)
            .where(SignalLog.timestamp >= since)
            .order_by(SignalLog.timestamp.desc())
            .limit(50)
        )
        return list(result.scalars())

    # ── MatchSnapshot ──────────────────────────────────────────────────────

    async def save_match_snapshot(
        self,
        match_id: str,
        player1_name: str,
        player2_name: str,
        surface: str,
        tournament: str,
        sets_p1: int,
        sets_p2: int,
        games_p1: int,
        games_p2: int,
        current_set: int,
        total_games_played: int,
        p1_momentum: int,
        odds_p1: float,
        odds_p2: float,
        model_win_prob_p1: float,
        model_win_prob_p2: float,
        serve_pct_p1: float,
        serve_pct_p2: float,
        game_log: list[int],
    ) -> None:
        self.session.add(MatchSnapshot(
            match_id=match_id,
            player1_name=player1_name,
            player2_name=player2_name,
            surface=surface,
            tournament=tournament,
            sets_p1=sets_p1,
            sets_p2=sets_p2,
            games_p1=games_p1,
            games_p2=games_p2,
            current_set=current_set,
            total_games_played=total_games_played,
            p1_momentum=p1_momentum,
            odds_p1=odds_p1,
            odds_p2=odds_p2,
            model_win_prob_p1=model_win_prob_p1,
            model_win_prob_p2=model_win_prob_p2,
            serve_pct_p1=serve_pct_p1,
            serve_pct_p2=serve_pct_p2,
            game_log_json=json.dumps(game_log),
            winner=0,
            timestamp=datetime.utcnow(),
        ))
        await self.session.commit()

    async def label_match_snapshots(self, match_id: str, winner: int) -> None:
        """Retroactively fill winner column on all snapshots for a completed match."""
        await self.session.execute(
            update(MatchSnapshot)
            .where(MatchSnapshot.match_id == match_id)
            .where(MatchSnapshot.winner == 0)
            .values(winner=winner)
        )
        await self.session.commit()

    # ── MatchCompletion ────────────────────────────────────────────────────

    async def save_match_completion(
        self,
        match_id: str,
        player1_name: str,
        player2_name: str,
        winner: int,
        final_sets_p1: int,
        final_sets_p2: int,
        final_score_str: str,
        tournament: str,
        surface: str,
        total_games: int,
        total_signals: int,
        signals_correct: int,
    ) -> None:
        existing = await self.session.get(MatchCompletion, match_id)
        if existing is not None:
            return  # already recorded
        self.session.add(MatchCompletion(
            match_id=match_id,
            player1_name=player1_name,
            player2_name=player2_name,
            winner=winner,
            final_sets_p1=final_sets_p1,
            final_sets_p2=final_sets_p2,
            final_score_str=final_score_str,
            tournament=tournament,
            surface=surface,
            total_games=total_games,
            total_signals_fired=total_signals,
            signals_correct=signals_correct,
            completed_at=datetime.utcnow(),
        ))
        await self.session.commit()

    # ── PlayerStats ────────────────────────────────────────────────────────

    async def get_player_stats(self, player_name: str, surface: str) -> PlayerStats | None:
        result = await self.session.execute(
            select(PlayerStats)
            .where(PlayerStats.player_name == player_name)
            .where(PlayerStats.surface == surface)
        )
        return result.scalar_one_or_none()

    # ── MatchResult (ML training data) ────────────────────────────────────

    async def save_match_result(
        self,
        match_id: str,
        features: "MatchFeatures",  # noqa: F821
        winner: int,
    ) -> None:
        self.session.add(MatchResult(
            match_id=match_id,
            p1_sets_lead=features.p1_sets_lead,
            p1_games_lead=features.p1_games_lead,
            current_set=features.current_set,
            p1_momentum=features.p1_momentum,
            p1_serve_pct=features.p1_serve_pct,
            p2_serve_pct=features.p2_serve_pct,
            surface_clay=features.surface_clay,
            surface_grass=features.surface_grass,
            surface_indoor=features.surface_indoor,
            match_progress=features.match_progress,
            p1_opening_implied=features.p1_opening_implied,
            winner=winner,
            recorded_at=datetime.utcnow(),
        ))
        await self.session.commit()

    async def get_training_data(self) -> tuple[np.ndarray, np.ndarray]:
        result = await self.session.execute(select(MatchResult))
        rows = list(result.scalars())
        if not rows:
            return np.empty((0, 11)), np.empty((0,))
        X = np.array([
            [
                r.p1_sets_lead, r.p1_games_lead, r.current_set, r.p1_momentum,
                r.p1_serve_pct, r.p2_serve_pct, r.surface_clay, r.surface_grass,
                r.surface_indoor, r.match_progress, r.p1_opening_implied,
            ]
            for r in rows
        ], dtype=float)
        y = np.array([r.winner for r in rows], dtype=int)
        return X, y

    # ── Maintenance ────────────────────────────────────────────────────────

    async def delete_old_odds_snapshots(self, days: int = 7) -> None:
        cutoff = datetime.utcnow() - timedelta(days=days)
        result = await self.session.execute(
            select(OddsSnapshot).where(OddsSnapshot.timestamp < cutoff)
        )
        for row in result.scalars():
            await self.session.delete(row)
        await self.session.commit()
