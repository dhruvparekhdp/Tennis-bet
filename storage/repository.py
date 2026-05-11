from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from storage.models import Match, OddsSnapshot, PlayerStats, SignalLog


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
    ) -> None:
        self.session.add(SignalLog(
            match_id=match_id, signal_type=signal_type, player_to_back=player_to_back,
            trigger_description=trigger_description, confidence=confidence,
            recommended_market=recommended_market, current_odds=current_odds,
            fair_odds=fair_odds, edge_pct=edge_pct, stake_pct=stake_pct,
            timestamp=datetime.utcnow(),
        ))
        await self.session.commit()

    async def get_last_signal_time(self, match_id: str, signal_type: str) -> datetime | None:
        result = await self.session.execute(
            select(SignalLog.timestamp)
            .where(SignalLog.match_id == match_id)
            .where(SignalLog.signal_type == signal_type)
            .order_by(SignalLog.timestamp.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return row

    # ── PlayerStats ────────────────────────────────────────────────────────

    async def get_player_stats(self, player_name: str, surface: str) -> PlayerStats | None:
        result = await self.session.execute(
            select(PlayerStats)
            .where(PlayerStats.player_name == player_name)
            .where(PlayerStats.surface == surface)
        )
        return result.scalar_one_or_none()

    # ── Maintenance ────────────────────────────────────────────────────────

    async def delete_old_odds_snapshots(self, days: int = 7) -> None:
        cutoff = datetime.utcnow() - timedelta(days=days)
        result = await self.session.execute(
            select(OddsSnapshot).where(OddsSnapshot.timestamp < cutoff)
        )
        for row in result.scalars():
            await self.session.delete(row)
        await self.session.commit()
