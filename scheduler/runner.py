"""
APScheduler-based 24/7 job runner.

Data collection strategy:
  - ESPN (primary)     → always works from cloud IPs, covers ATP + WTA live scores
  - Sofascore (enrich) → attempted for serve stats only; silently skipped if blocked

Jobs:
  - data_poll:      every 30s  → ESPN fetch + optional Sofascore + analysis + snapshots
  - schedule_poll:  every 5min → TheSportsDB schedule
  - db_cleanup:     daily      → delete old odds snapshots
  - heartbeat:      every 10m  → log status

Data storage:
  - MatchSnapshot: saved every ~2 minutes per match (every 4 polls)
  - MatchCompletion: saved when a match disappears from the live feed
  - SignalLog: updated with outcome (won/lost) when match completes
"""
import json
import os
from datetime import datetime, timezone

import httpx
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from analysis.engine import AnalysisEngine
from analysis.match_state import MatchState
from analysis.ml_predictor import MLPredictor
from analysis.state_store import MatchStateStore
from analysis.win_probability import compute_win_probability
from collectors.bets_api import BetsAPICollector
from collectors.espn import ESPNCollector
from collectors.flashscore import FlashscoreCollector
from collectors.historical_importer import run_import
from collectors.odds_api import OddsApiCollector
from collectors.slam_pbp_importer import run_slam_import
from collectors.sofascore import SofascoreCollector
from collectors.thesportsdb import TheSportsDBCollector
from config.settings import settings
from notifications.telegram_notifier import TelegramNotifier
from storage.database import AsyncSessionFactory
from storage.repository import Repository

log = structlog.get_logger()

# Save a snapshot every this many data polls (30s * 4 = ~2 minutes)
_SNAPSHOT_EVERY_N_POLLS = 4


def _infer_winner(state: MatchState) -> int | None:
    """Infer match winner from final sets score. Returns None if inconclusive."""
    if state.sets_p1 > state.sets_p2:
        return 1
    if state.sets_p2 > state.sets_p1:
        return 2
    return None


def _format_score(state: MatchState) -> str:
    return f"{state.sets_p1}-{state.sets_p2} sets ({state.games_in_set_p1}-{state.games_in_set_p2} current)"


class AppRunner:
    def __init__(self) -> None:
        self.store = MatchStateStore()
        self.flashscore = FlashscoreCollector(self.store)
        self.espn = ESPNCollector(self.store)
        self.sofascore = SofascoreCollector(self.store)
        self.thesportsdb = TheSportsDBCollector(api_key=settings.thesportsdb_api_key)
        self.odds_api = OddsApiCollector(self.store)
        self.bets_api = BetsAPICollector(self.store)
        self.ml_predictor = MLPredictor()
        self.notifier = TelegramNotifier()
        self.scheduler = AsyncIOScheduler()
        # Persistent engine so _cooldowns dict survives across poll cycles
        self._engine: AnalysisEngine | None = None
        # Track last-seen match states to detect completions
        self._last_states: dict[str, MatchState] = {}
        # Per-match poll counter for snapshot throttling
        self._poll_counters: dict[str, int] = {}

    async def _data_poll_job(self) -> None:
        await self.flashscore.fetch()
        await self.espn.fetch()
        await self.bets_api.fetch()

        if self.sofascore._consecutive_failures < 5:
            await self.sofascore.fetch()

        current_states = {s.match_id: s for s in await self.store.get_all()}

        # Detect matches that just completed (were live last poll, gone now)
        completed_ids = set(self._last_states) - set(current_states)
        if completed_ids:
            await self._handle_completions(completed_ids)

        # Run analysis + take snapshots
        await self._run_analysis(current_states)

        self._last_states = current_states

    async def _handle_completions(self, completed_ids: set[str]) -> None:
        """Process matches that disappeared from the live feed."""
        async with AsyncSessionFactory() as session:
            repo = Repository(session)
            for match_id in completed_ids:
                state = self._last_states[match_id]
                winner = _infer_winner(state)
                if winner is None:
                    log.info("match_completion_inconclusive", match_id=match_id,
                             sets=f"{state.sets_p1}-{state.sets_p2}")
                    continue

                total_sigs, correct_sigs = await repo.update_signal_outcomes(match_id, winner)
                await repo.label_match_snapshots(match_id, winner)
                await repo.save_match_completion(
                    match_id=match_id,
                    player1_name=state.player1_name,
                    player2_name=state.player2_name,
                    winner=winner,
                    final_sets_p1=state.sets_p1,
                    final_sets_p2=state.sets_p2,
                    final_score_str=_format_score(state),
                    tournament=state.tournament,
                    surface=state.surface,
                    total_games=state.total_games_played(),
                    total_signals=total_sigs,
                    signals_correct=correct_sigs,
                )
                await repo.mark_match_finished(match_id)

                winner_name = state.player1_name if winner == 1 else state.player2_name
                accuracy = f"{correct_sigs}/{total_sigs}" if total_sigs > 0 else "no signals"
                log.info(
                    "match_completed",
                    match_id=match_id,
                    winner=winner_name,
                    score=_format_score(state),
                    signal_accuracy=accuracy,
                )
                # Clean up poll counter
                self._poll_counters.pop(match_id, None)

    async def _run_analysis(self, current_states: dict[str, MatchState]) -> None:
        if not current_states:
            return
        async with AsyncSessionFactory() as session:
            repo = Repository(session)
            if self._engine is None:
                self._engine = AnalysisEngine(repo)
            else:
                self._engine.repository = repo

            for match_id, state in current_states.items():
                try:
                    # Run signal analysis
                    signals = await self._engine.process(state)
                    for sig in signals:
                        await self.notifier.send_signal(sig)
                        log.info(
                            "signal_fired",
                            signal_type=sig.signal_type,
                            match_id=sig.match_id,
                            player=sig.player_name,
                            confidence=sig.confidence,
                        )

                    # Take periodic snapshot (every N polls per match)
                    counter = self._poll_counters.get(match_id, 0) + 1
                    self._poll_counters[match_id] = counter
                    if counter % _SNAPSHOT_EVERY_N_POLLS == 0:
                        await self._save_snapshot(repo, state)

                except Exception:
                    log.exception("analysis_job_failed", match_id=match_id)

    async def _save_snapshot(self, repo: Repository, state: MatchState) -> None:
        """Save a periodic match state snapshot for ML training."""
        try:
            model_p1, model_p2 = compute_win_probability(state)
            # Momentum: positive = p1 streak, negative = p2 streak
            p1_streak = state.consecutive_games_won_by(1)
            p2_streak = state.consecutive_games_won_by(2)
            momentum = p1_streak if p1_streak > 0 else -p2_streak

            await repo.save_match_snapshot(
                match_id=state.match_id,
                player1_name=state.player1_name,
                player2_name=state.player2_name,
                surface=state.surface,
                tournament=state.tournament,
                sets_p1=state.sets_p1,
                sets_p2=state.sets_p2,
                games_p1=state.games_in_set_p1,
                games_p2=state.games_in_set_p2,
                current_set=state.current_set,
                total_games_played=state.total_games_played(),
                p1_momentum=momentum,
                odds_p1=state.odds_p1,
                odds_p2=state.odds_p2,
                model_win_prob_p1=round(model_p1, 4),
                model_win_prob_p2=round(model_p2, 4),
                serve_pct_p1=state.serve_stats_p1.first_serve_pct,
                serve_pct_p2=state.serve_stats_p2.first_serve_pct,
                game_log=state.game_log,
            )
        except Exception:
            log.exception("snapshot_save_failed", match_id=state.match_id)

    async def _odds_job(self) -> None:
        try:
            await self.odds_api.fetch()
        except Exception:
            log.exception("odds_job_failed")

    async def _ml_retrain_job(self) -> None:
        try:
            async with AsyncSessionFactory() as session:
                repo = Repository(session)
                await self.ml_predictor.maybe_retrain(repo)
        except Exception:
            log.exception("ml_retrain_job_failed")

    async def _schedule_job(self) -> None:
        await self.thesportsdb.fetch()

    async def _historical_import_job(self) -> None:
        # Only run match-level import on cloud — slam PBP (2.5M rows) is local-only
        # Run scripts/scrape_history.py on your laptop for slam point-by-point data
        try:
            async with AsyncSessionFactory() as session:
                await run_import(session)
        except Exception:
            log.exception("historical_import_failed_non_fatal")

    async def _cleanup_job(self) -> None:
        async with AsyncSessionFactory() as session:
            repo = Repository(session)
            await repo.delete_old_odds_snapshots(days=7)
        log.info("db_cleanup_done")

    async def _heartbeat_job(self) -> None:
        count = await self.store.count()
        sofascore_ok = self.sofascore._consecutive_failures == 0
        flashscore_ok = self.flashscore._consecutive_failures == 0
        log.info(
            "heartbeat",
            matches_tracked=count,
            sofascore_available=sofascore_ok,
            flashscore_http_ok=flashscore_ok,
            flashscore_consecutive_zeros=self.flashscore._consecutive_zero_matches,
        )

    async def _self_ping_job(self) -> None:
        port = int(os.environ.get("PORT", 8080))
        url = f"http://localhost:{port}/health"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
            log.debug("self_ping_ok", status=resp.status_code)
        except Exception as exc:
            log.warning("self_ping_failed", error=str(exc))

    def setup_jobs(self) -> None:
        self.scheduler.add_job(
            self._data_poll_job,
            "interval",
            seconds=settings.sofascore_poll_interval,
            id="data_poll",
            max_instances=1,
        )
        self.scheduler.add_job(
            self._schedule_job,
            "interval",
            seconds=settings.schedule_poll_interval,
            id="schedule_poll",
            max_instances=1,
        )
        self.scheduler.add_job(
            self._odds_job,
            "interval",
            seconds=settings.odds_poll_interval_seconds,
            id="odds_poll",
            max_instances=1,
            next_run_time=datetime.now(timezone.utc),
        )
        self.scheduler.add_job(
            self._ml_retrain_job,
            "interval",
            hours=6,
            id="ml_retrain",
            max_instances=1,
        )
        self.scheduler.add_job(
            self._cleanup_job,
            "cron",
            hour=3,
            minute=0,
            id="db_cleanup",
        )
        self.scheduler.add_job(
            self._heartbeat_job,
            "interval",
            minutes=10,
            id="heartbeat",
        )
        self.scheduler.add_job(
            self._self_ping_job,
            "interval",
            minutes=5,
            id="self_ping",
            max_instances=1,
        )
        # Historical import — runs immediately on startup, then weekly
        self.scheduler.add_job(
            self._historical_import_job,
            "interval",
            weeks=1,
            id="historical_import",
            max_instances=1,
            next_run_time=datetime.now(timezone.utc),
        )

    async def start(self) -> None:
        await self.notifier.verify()
        self.setup_jobs()
        self.scheduler.start()
        bets_api_status = "BetsAPI: active" if settings.bets_api_token else "BetsAPI: no token"
        await self.notifier.send_text(
            "🎾 Tennis-bet monitor started.\n"
            f"Polling every {settings.sofascore_poll_interval}s | "
            f"Min confidence: {settings.min_confidence} | "
            f"Data: Flashscore + ESPN + {bets_api_status} → Sofascore (serve stats)"
        )
        log.info("scheduler_started")

    def get_status(self) -> dict:
        return {
            "flashscore": {
                "http_ok": self.flashscore._consecutive_failures == 0,
                "consecutive_failures": self.flashscore._consecutive_failures,
                "consecutive_zeros": self.flashscore._consecutive_zero_matches,
            },
            "espn": {"ok": True},
            "sofascore": {
                "blocked": self.sofascore._consecutive_failures >= 5,
                "consecutive_failures": self.sofascore._consecutive_failures,
            },
            "odds_api": {
                "key_set": bool(settings.odds_api_key),
                "poll_interval_secs": settings.odds_poll_interval_seconds,
            },
            "bets_api": {
                "token_set": bool(settings.bets_api_token),
                "consecutive_failures": self.bets_api._consecutive_failures,
            },
        }

    async def stop(self) -> None:
        self.scheduler.shutdown(wait=False)
        await self.sofascore.close()
        await self.notifier.send_text("Tennis-bet monitor stopped.")
        log.info("scheduler_stopped")
