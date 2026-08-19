from __future__ import annotations

import json
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from storage.models import (
    CommoditySnapshot, CryptoSignalLog, CryptoSnapshot, CryptoWatchlistEntry,
    Match, MatchCompletion, MatchResult, MatchSnapshot,
    OddsSnapshot, PaperCycle, PaperPosition, PaperTrade, PlayerStats, SignalLog,
)


class Repository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ── Match ──────────────────────────────────────────────────

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

    # ── OddsSnapshot ───────────────────────────────────────────

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

    # ── SignalLog ────────────────────────────────────────────

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

    # ── MatchSnapshot ──────────────────────────────────────────

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

    # ── MatchCompletion ─────────────────────────────────────────

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

    # ── PlayerStats ──────────────────────────────────────────

    async def get_player_stats(self, player_name: str, surface: str) -> PlayerStats | None:
        result = await self.session.execute(
            select(PlayerStats)
            .where(PlayerStats.player_name == player_name)
            .where(PlayerStats.surface == surface)
        )
        return result.scalar_one_or_none()

    # ── MatchResult (ML training data) ──────────────────────────

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

    # ── Maintenance ────────────────────────────────────────────

    async def delete_old_odds_snapshots(self, days: int = 7) -> None:
        cutoff = datetime.utcnow() - timedelta(days=days)
        result = await self.session.execute(
            select(OddsSnapshot).where(OddsSnapshot.timestamp < cutoff)
        )
        for row in result.scalars():
            await self.session.delete(row)
        await self.session.commit()

    async def delete_old_crypto_data(self, days: int = 7) -> None:
        """Bound the growth of crypto/commodity snapshots + old signal log rows.

        These are written every crypto_snapshot_interval_seconds (default 2 min)
        for every watchlist symbol — left unbounded they'd eventually fill a
        free-tier Postgres instance, same as odds_snapshots would without this.
        """
        cutoff = datetime.utcnow() - timedelta(days=days)
        await self.session.execute(delete(CryptoSnapshot).where(CryptoSnapshot.timestamp < cutoff))
        await self.session.execute(delete(CommoditySnapshot).where(CommoditySnapshot.timestamp < cutoff))
        await self.session.execute(delete(CryptoSignalLog).where(CryptoSignalLog.timestamp < cutoff))
        await self.session.commit()

    async def get_h2h(
        self, player1: str, player2: str, surface: str | None = None
    ) -> dict:
        """Head-to-head record from MatchRecord historical data. Uses last-name matching."""
        from sqlalchemy import or_, and_
        p1_last = player1.strip().split()[-1].lower()
        p2_last = player2.strip().split()[-1].lower()

        q = (
            select(MatchRecord)
            .where(
                or_(
                    and_(
                        MatchRecord.winner_name.ilike(f"%{p1_last}%"),
                        MatchRecord.loser_name.ilike(f"%{p2_last}%"),
                    ),
                    and_(
                        MatchRecord.winner_name.ilike(f"%{p2_last}%"),
                        MatchRecord.loser_name.ilike(f"%{p1_last}%"),
                    ),
                )
            )
            .order_by(MatchRecord.year.desc())
            .limit(30)
        )
        result = await self.session.execute(q)
        records = list(result.scalars().all())

        p1_wins = sum(1 for r in records if p1_last in r.winner_name.lower())
        p2_wins = len(records) - p1_wins

        on_surface = [r for r in records if r.surface.lower() == (surface or "").lower()] if surface else []
        p1_sw = sum(1 for r in on_surface if p1_last in r.winner_name.lower())
        p2_sw = len(on_surface) - p1_sw

        last_meetings = []
        for r in records[:8]:
            winner_is_p1 = p1_last in r.winner_name.lower()
            last_meetings.append({
                "year": r.year,
                "tournament": r.tourney_name,
                "surface": r.surface,
                "round": r.round,
                "winner": "p1" if winner_is_p1 else "p2",
                "winner_name": r.winner_name,
                "loser_name": r.loser_name,
                "score": r.score,
            })

        return {
            "total_meetings": len(records),
            "p1_wins": p1_wins,
            "p2_wins": p2_wins,
            "surface_meetings": len(on_surface),
            "p1_surface_wins": p1_sw,
            "p2_surface_wins": p2_sw,
            "last_meetings": last_meetings,
        }

    async def get_player_form(self, player_name: str, surface: str | None = None, n: int = 15) -> dict:
        """Last n matches for a player with win/loss, surface stats, serve averages."""
        from sqlalchemy import or_
        last = player_name.strip().split()[-1].lower()

        q = (
            select(MatchRecord)
            .where(
                or_(
                    MatchRecord.winner_name.ilike(f"%{last}%"),
                    MatchRecord.loser_name.ilike(f"%{last}%"),
                )
            )
            .order_by(MatchRecord.year.desc())
            .limit(n)
        )
        result = await self.session.execute(q)
        records = list(result.scalars().all())

        matches = []
        total_aces, total_first_svpt, total_svpt, total_bp_saved, total_bp_faced = 0, 0, 0, 0, 0
        for r in records:
            won = last in r.winner_name.lower()
            if won:
                aces, svpt, first_in = r.w_ace, r.w_svpt, r.w_1st_in
                bp_saved, bp_faced = r.w_bp_saved, r.w_bp_faced
                opponent = r.loser_name
            else:
                aces, svpt, first_in = r.l_ace, r.l_svpt, r.l_1st_in
                bp_saved, bp_faced = r.l_bp_saved, r.l_bp_faced
                opponent = r.winner_name

            fsp = round(first_in / svpt * 100) if svpt > 0 else 0
            matches.append({
                "year": r.year, "tournament": r.tourney_name[:22],
                "surface": r.surface, "won": won,
                "opponent": opponent.split()[-1] if opponent else "?",
                "score": r.score[:20], "aces": aces, "first_serve_pct": fsp,
            })
            total_aces += aces
            total_first_svpt += first_in
            total_svpt += svpt
            total_bp_saved += bp_saved
            total_bp_faced += bp_faced

        wins = sum(1 for m in matches if m["won"])
        n_matches = len(matches)

        # Surface-specific win rate
        surf_matches = [m for m in matches if m["surface"].lower() == (surface or "").lower()] if surface else []
        surf_wins = sum(1 for m in surf_matches if m["won"])

        return {
            "recent": matches[:10],
            "wins": wins,
            "total": n_matches,
            "win_rate": round(wins / n_matches * 100) if n_matches > 0 else 0,
            "surface_win_rate": round(surf_wins / len(surf_matches) * 100) if surf_matches else None,
            "avg_aces": round(total_aces / n_matches, 1) if n_matches > 0 else 0,
            "avg_first_serve_pct": round(total_first_svpt / total_svpt * 100) if total_svpt > 0 else 0,
            "bp_save_pct": round(total_bp_saved / total_bp_faced * 100) if total_bp_faced > 0 else 0,
        }

    # ── Crypto & Commodities ────────────────────────────────────

    async def save_crypto_snapshot(
        self,
        symbol: str,
        price: float,
        volume_24h: float,
        rsi_14: float,
        macd_line: float,
        macd_signal: float,
        bollinger_upper: float,
        bollinger_lower: float,
        atr_14: float,
        sentiment_score: float,
    ) -> None:
        self.session.add(CryptoSnapshot(
            symbol=symbol,
            price=price,
            volume_24h=volume_24h,
            rsi_14=rsi_14,
            macd_line=macd_line,
            macd_signal=macd_signal,
            bollinger_upper=bollinger_upper,
            bollinger_lower=bollinger_lower,
            atr_14=atr_14,
            sentiment_score=sentiment_score,
            timestamp=datetime.utcnow(),
        ))
        await self.session.commit()

    async def save_commodity_snapshot(
        self,
        symbol: str,
        price: float,
        rsi_14: float,
        atr_14: float,
    ) -> None:
        self.session.add(CommoditySnapshot(
            symbol=symbol,
            price=price,
            rsi_14=rsi_14,
            atr_14=atr_14,
            timestamp=datetime.utcnow(),
        ))
        await self.session.commit()

    async def log_crypto_signal(
        self,
        symbol: str,
        signal_type: str,
        direction: str,
        trigger_description: str,
        confidence: float,
        current_price: float,
        target_price: float | None,
        stop_loss: float | None,
        edge_pct: float,
        stake_pct: float,
        timeframe: str,
        sentiment_score: float = 0.0,
        indicators_summary: str = "",
    ) -> None:
        self.session.add(CryptoSignalLog(
            symbol=symbol,
            signal_type=signal_type,
            direction=direction,
            trigger_description=trigger_description,
            confidence=confidence,
            current_price=current_price,
            target_price=target_price or 0.0,
            stop_loss=stop_loss or 0.0,
            edge_pct=edge_pct,
            stake_pct=stake_pct,
            timeframe=timeframe,
            sentiment_score=sentiment_score,
            indicators_summary=indicators_summary,
            outcome="pending",
            pnl_pct=0.0,
            timestamp=datetime.utcnow(),
        ))
        await self.session.commit()

    async def get_recent_crypto_signals(self, hours: int = 24) -> list[CryptoSignalLog]:
        since = datetime.utcnow() - timedelta(hours=hours)
        result = await self.session.execute(
            select(CryptoSignalLog)
            .where(CryptoSignalLog.timestamp >= since)
            .order_by(CryptoSignalLog.timestamp.desc())
            .limit(50)
        )
        return list(result.scalars())

    # ── Crypto watchlist (DB-backed, editable at runtime without a redeploy) ─

    async def get_crypto_watchlist(self) -> list[str]:
        result = await self.session.execute(select(CryptoWatchlistEntry.symbol))
        return [row[0] for row in result.all()]

    async def add_crypto_watchlist_symbol(self, symbol: str) -> None:
        sym = symbol.strip().lower()
        existing = await self.session.get(CryptoWatchlistEntry, sym)
        if existing is None:
            self.session.add(CryptoWatchlistEntry(symbol=sym, added_at=datetime.utcnow()))
            await self.session.commit()

    async def remove_crypto_watchlist_symbol(self, symbol: str) -> None:
        sym = symbol.strip().lower()
        existing = await self.session.get(CryptoWatchlistEntry, sym)
        if existing is not None:
            await self.session.delete(existing)
            await self.session.commit()

    async def seed_crypto_watchlist_if_empty(self, default_symbols: list[str]) -> list[str]:
        """First-run only: populate the table from the small seed list.

        Returns the active watchlist either way, so the caller can always use
        the return value regardless of whether seeding happened.
        """
        existing = await self.get_crypto_watchlist()
        if existing:
            return existing
        symbols = [s.strip().lower() for s in default_symbols if s.strip()]
        for sym in symbols:
            self.session.add(CryptoWatchlistEntry(symbol=sym, added_at=datetime.utcnow()))
        await self.session.commit()
        return symbols

    # ── Paper trading ─────────────────────────────────────────────────────

    async def get_running_cycle(self) -> PaperCycle | None:
        res = await self.session.execute(
            select(PaperCycle).where(PaperCycle.status == "running")
            .order_by(PaperCycle.id.desc()).limit(1)
        )
        return res.scalar_one_or_none()

    async def start_cycle(
        self,
        starting_wallet: float,
        target_wallet: float,
        leverage: float,
        stop_pct_of_margin: float,
        reward_risk: float,
        min_confidence: float,
        trailing_enabled: bool,
        scaled_sizing: bool,
        scaled_leverage: bool = False,
    ) -> PaperCycle:
        """
        Begin a cycle, recording the configuration it runs under.

        Storing the settings on the row rather than reading them from the
        environment is what makes cycles comparable: a cycle you re-read next
        month still knows the leverage and risk it was actually run with.
        """
        cycle = PaperCycle(
            started_at=datetime.utcnow(),
            starting_wallet=starting_wallet,
            target_wallet=target_wallet,
            wallet=starting_wallet,
            peak_wallet=starting_wallet,
            leverage=leverage,
            stop_pct_of_margin=stop_pct_of_margin,
            reward_risk=reward_risk,
            min_confidence=min_confidence,
            trailing_enabled=trailing_enabled,
            scaled_sizing=scaled_sizing,
            scaled_leverage=scaled_leverage,
            status="running",
        )
        self.session.add(cycle)
        await self.session.commit()
        await self.session.refresh(cycle)
        return cycle

    async def update_cycle_wallet(self, cycle_id: int, wallet: float) -> None:
        cycle = await self.session.get(PaperCycle, cycle_id)
        if cycle is None:
            return
        cycle.wallet = wallet
        cycle.peak_wallet = max(cycle.peak_wallet, wallet)
        await self.session.commit()

    async def end_cycle(self, cycle_id: int, status: str, note: str = "") -> None:
        cycle = await self.session.get(PaperCycle, cycle_id)
        if cycle is None:
            return
        cycle.status = status
        cycle.note = note
        cycle.ended_at = datetime.utcnow()
        await self.session.commit()

    async def get_open_positions(self, cycle_id: int) -> list[PaperPosition]:
        res = await self.session.execute(
            select(PaperPosition).where(PaperPosition.cycle_id == cycle_id)
            .order_by(PaperPosition.id)
        )
        return list(res.scalars().all())

    async def save_position(self, cycle_id: int, pos) -> PaperPosition:
        row = PaperPosition(
            cycle_id=cycle_id,
            symbol=pos.symbol,
            side=pos.side.value,
            signal_price=pos.signal_price,
            entry_price=pos.entry_price,
            margin=pos.margin,
            leverage=pos.leverage,
            coin_qty=pos.coin_qty,
            usdt_inr=pos.usdt_inr,
            stop_price=pos.stop_price,
            initial_stop_price=pos.initial_stop_price,
            target_price=pos.target_price,
            liq_price=pos.liq_price,
            peak_price=pos.peak_price,
            trail_active=pos.trail_active,
            entry_fee=pos.entry_fee,
            signal_type=pos.signal_type,
            timeframe=pos.timeframe,
            confidence=pos.confidence,
            opened_at=pos.opened_at.replace(tzinfo=None),
            expires_at=pos.expires_at.replace(tzinfo=None) if pos.expires_at else None,
        )
        self.session.add(row)
        await self.session.commit()
        await self.session.refresh(row)
        return row

    async def sync_position(self, row_id: int, pos) -> None:
        """Persist trail movement. Called every tick, so it writes only what moves."""
        row = await self.session.get(PaperPosition, row_id)
        if row is None:
            return
        row.stop_price = pos.stop_price
        row.target_price = pos.target_price
        row.peak_price = pos.peak_price
        row.trail_active = pos.trail_active
        await self.session.commit()

    async def delete_position(self, row_id: int) -> None:
        row = await self.session.get(PaperPosition, row_id)
        if row is not None:
            await self.session.delete(row)
            await self.session.commit()

    async def record_trade(self, cycle_id: int, trade) -> None:
        pos = trade.position
        self.session.add(PaperTrade(
            cycle_id=cycle_id,
            symbol=pos.symbol,
            side=pos.side.value,
            signal_price=pos.signal_price,
            entry_price=pos.entry_price,
            exit_price=trade.exit_price,
            coin_qty=pos.coin_qty,
            margin=pos.margin,
            leverage=pos.leverage,
            stop_price=pos.stop_price,
            target_price=pos.target_price,
            exit_reason=trade.reason.value,
            gross_pnl=trade.gross_pnl,
            # Fees and funding stay separate so "was it the strategy or the
            # costs" is still answerable after the fact.
            trading_fees=trade.fees_paid - trade.funding_paid,
            funding_paid=trade.funding_paid,
            net_pnl=trade.net_pnl,
            return_on_margin=trade.return_on_margin,
            wallet_after=trade.wallet_after,
            signal_type=pos.signal_type,
            timeframe=pos.timeframe,
            confidence=pos.confidence,
            entry_slippage_pct=trade.entry_slippage_pct,
            hours_held=trade.hours_held,
            opened_at=pos.opened_at.replace(tzinfo=None),
            closed_at=trade.closed_at.replace(tzinfo=None),
        ))
        await self.session.commit()

    async def get_cycle_trades(self, cycle_id: int, limit: int = 500) -> list[PaperTrade]:
        res = await self.session.execute(
            select(PaperTrade).where(PaperTrade.cycle_id == cycle_id)
            .order_by(PaperTrade.closed_at.desc()).limit(limit)
        )
        return list(res.scalars().all())

    async def get_recent_cycles(self, limit: int = 20) -> list[PaperCycle]:
        res = await self.session.execute(
            select(PaperCycle).order_by(PaperCycle.id.desc()).limit(limit)
        )
        return list(res.scalars().all())
