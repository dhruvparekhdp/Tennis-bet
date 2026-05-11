from datetime import datetime, timedelta

import structlog

from analysis.fatigue import FatigueAnalyzer
from analysis.match_state import MatchState
from analysis.momentum import MomentumAnalyzer
from analysis.odds_value import OddsValueAnalyzer
from analysis.server_performance import ServerPerformanceAnalyzer
from analysis.set_patterns import SetPatternAnalyzer
from analysis.signal import Signal
from config.settings import settings
from storage.models import PlayerStats
from storage.repository import Repository

log = structlog.get_logger()


class AnalysisEngine:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self.momentum = MomentumAnalyzer()
        self.odds_value = OddsValueAnalyzer()
        self.server_perf = ServerPerformanceAnalyzer()
        self.set_patterns = SetPatternAnalyzer()
        self.fatigue = FatigueAnalyzer()
        # In-memory cooldown cache: (match_id, signal_type) → last_sent datetime
        self._cooldowns: dict[tuple[str, str], datetime] = {}

    async def process(self, state: MatchState) -> list[Signal]:
        """Run all analyzers against the given match state, return signals that pass
        confidence threshold and cooldown checks."""
        player_stats = await self._load_player_stats(state)

        candidates: list[Signal | None] = [
            self.momentum.analyze(state),
            self.odds_value.analyze(state),
            self.server_perf.analyze(state),
            self.set_patterns.analyze(state, player_stats),
            self.fatigue.analyze(state),
        ]

        fired: list[Signal] = []
        for sig in candidates:
            if sig is None:
                continue
            if sig.confidence < settings.min_confidence:
                log.debug("signal_below_threshold",
                          signal_type=sig.signal_type,
                          confidence=sig.confidence,
                          match=f"{sig.player_name} vs {sig.opponent_name}")
                continue
            if self._is_on_cooldown(state.match_id, sig.signal_type):
                log.debug("signal_on_cooldown", signal_type=sig.signal_type, match_id=state.match_id)
                continue

            self._set_cooldown(state.match_id, sig.signal_type)
            await self._log_signal(sig)
            fired.append(sig)

        return fired

    def _is_on_cooldown(self, match_id: str, signal_type: str) -> bool:
        key = (match_id, signal_type)
        last = self._cooldowns.get(key)
        if last is None:
            return False
        cooldown = timedelta(minutes=settings.signal_cooldown_minutes)
        return datetime.utcnow() - last < cooldown

    def _set_cooldown(self, match_id: str, signal_type: str) -> None:
        self._cooldowns[(match_id, signal_type)] = datetime.utcnow()

    async def _log_signal(self, sig: Signal) -> None:
        try:
            await self.repository.log_signal(
                match_id=sig.match_id,
                signal_type=sig.signal_type,
                player_to_back=sig.player_to_back,
                trigger_description=sig.trigger_description,
                confidence=sig.confidence,
                recommended_market=sig.recommended_market,
                current_odds=sig.current_odds,
                fair_odds=sig.fair_odds,
                edge_pct=sig.edge_pct,
                stake_pct=sig.stake_pct,
            )
        except Exception:
            log.exception("failed_to_log_signal", signal_type=sig.signal_type)

    async def _load_player_stats(self, state: MatchState) -> dict[str, PlayerStats | None]:
        stats: dict[str, PlayerStats | None] = {}
        for name in (state.player1_name, state.player2_name):
            try:
                stats[name] = await self.repository.get_player_stats(name, state.surface)
            except Exception:
                stats[name] = None
        return stats
