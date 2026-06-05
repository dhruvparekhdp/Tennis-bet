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
from analysis.football_engine import FootballEngine
from analysis.football_state import FootballStateStore
from analysis.match_state import MatchState
from analysis.ml_predictor import MLPredictor
from analysis.scalping import scan_all
from analysis.state_store import MatchStateStore
from analysis.win_probability import compute_win_probability
from collectors.api_sports import ApiSportsCollector
from collectors.bets_api import BetsAPICollector
from collectors.espn import ESPNCollector
from collectors.flashscore import FlashscoreCollector
from collectors.football_espn import FootballESPNCollector
from collectors.football_odds_api import FootballOddsApiCollector
from collectors.sportradar import SportradarCollector
from collectors.historical_importer import run_import
from collectors.odds_api import OddsApiCollector
from collectors.slam_pbp_importer import run_slam_import
from collectors.sofascore import SofascoreCollector
from collectors.thesportsdb import TheSportsDBCollector
from config.settings import settings
from notifications.football_formatter import format_football_signal
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
        self.api_sports = ApiSportsCollector(self.store)
        self.ml_predictor = MLPredictor()
        self.notifier = TelegramNotifier()
        self.scheduler = AsyncIOScheduler()
        # Tennis: persistent engine so _cooldowns survive across poll cycles
        self._engine: AnalysisEngine | None = None
        # Track last-seen match states to detect completions
        self._last_states: dict[str, MatchState] = {}
        # Per-match poll counter for snapshot throttling
        self._poll_counters: dict[str, int] = {}
        # Football
        self.football_store = FootballStateStore()
        self.football_espn = FootballESPNCollector(self.football_store)
        self.football_odds = FootballOddsApiCollector(self.football_store)
        self.football_engine = FootballEngine()
        # Sportradar — covers ALL tennis (Challengers, ITF) + ALL football in one call each
        self.sportradar = SportradarCollector(self.store, self.football_store)
        # Scalping alerts — track last Telegram ping per match to avoid spam
        self._scalp_alert_times: dict[str, datetime] = {}
        # Collector enable/disable toggles (runtime, not persisted across restarts)
        self.collector_enabled: dict[str, bool] = {
            "sportradar": True,
            "odds_api": True,
            "api_sports": True,
            "espn": True,
            "bets_api": True,
        }

    async def _data_poll_job(self) -> None:
        await self.flashscore.fetch()
        if self.collector_enabled.get("espn", True):
            await self.espn.fetch()
        if self.collector_enabled.get("bets_api", True):
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

    async def _football_poll_job(self) -> None:
        try:
            await self.football_espn.fetch()
            # Enrich with Odds API odds + upcoming matches (if key configured)
            if settings.odds_api_key:
                try:
                    await self.football_odds.fetch(settings.odds_api_key)
                except Exception:
                    log.exception("football_odds_api_failed")
            for state in await self.football_store.get_all():
                if state.is_scheduled:
                    continue  # don't run signals on upcoming matches
                try:
                    signals = self.football_engine.process(state)
                    for sig in signals:
                        msg = format_football_signal(sig)
                        await self.notifier.send_text(msg)
                except Exception:
                    log.exception("football_analysis_failed", match_id=state.match_id)
        except Exception:
            log.exception("football_poll_job_failed")

    async def _odds_job(self) -> None:
        if not self.collector_enabled.get("odds_api", True):
            log.debug("odds_api_skipped_disabled")
            return
        try:
            await self.odds_api.fetch()
        except Exception:
            log.exception("odds_job_failed")

    async def _scalp_job(self) -> None:
        """Scan live tennis for sure-shot 'lock' scalps and ping Telegram (deduped)."""
        if not settings.scalp_alert_telegram:
            return
        try:
            states = await self.store.get_all()
            opps = scan_all(
                states,
                min_win_prob=settings.scalp_min_win_prob,
                lock_win_prob=settings.scalp_lock_win_prob,
                max_odds=settings.scalp_max_odds,
                lock_max_odds=settings.scalp_lock_max_odds,
            )
            now = datetime.now(timezone.utc)
            cooldown = settings.scalp_alert_cooldown_minutes * 60
            for o in opps:
                if o.tier != "lock":
                    continue
                last = self._scalp_alert_times.get(o.match_id)
                if last and (now - last).total_seconds() < cooldown:
                    continue
                self._scalp_alert_times[o.match_id] = now
                odds_txt = f"{o.market_odds:.2f}" if o.market_odds > 1.01 else "n/a"
                ev_txt = f"{o.ev_pct:+.1f}%" if o.market_odds > 1.01 else "n/a"
                reasons = ", ".join(o.reasons) if o.reasons else "decisive lead"
                window = "\n⚡ SCALP WINDOW — odds drifted up, better entry now" if o.scalp_window else ""
                await self.notifier.send_text(
                    f"🔒 SURE-SHOT SCALP\n"
                    f"Back: {o.player_name}\n"
                    f"vs {o.opponent_name}\n"
                    f"{o.tournament} ({o.surface})\n"
                    f"Score: {o.score_summary}\n"
                    f"Win prob: {o.win_prob*100:.0f}% · Odds: {odds_txt} · EV: {ev_txt}\n"
                    f"Why: {reasons}{window}"
                )
                log.info("scalp_alert_sent", match_id=o.match_id,
                         player=o.player_name, win_prob=round(o.win_prob, 3))
            # Drop stale alert-time entries for matches no longer live
            live_ids = {s.match_id for s in states}
            for mid in list(self._scalp_alert_times):
                if mid not in live_ids:
                    self._scalp_alert_times.pop(mid, None)
        except Exception:
            log.exception("scalp_job_failed")

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

    async def _api_sports_job(self) -> None:
        if not settings.api_sports_key:
            return
        if not self.collector_enabled.get("api_sports", True):
            log.debug("api_sports_skipped_disabled")
            return
        try:
            await self.api_sports.fetch()
        except Exception:
            log.exception("api_sports_job_failed")

    async def _sportradar_job(self) -> None:
        key = settings.sportradar_api_key
        if not key:
            return
        if not self.collector_enabled.get("sportradar", True):
            log.debug("sportradar_skipped_disabled")
            return
        try:
            await self.sportradar.fetch_tennis(key)
        except Exception:
            log.exception("sportradar_tennis_job_failed")
        try:
            await self.sportradar.fetch_soccer(key)
        except Exception:
            log.exception("sportradar_soccer_job_failed")

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
            next_run_time=datetime.now(timezone.utc),  # run immediately on startup
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
        if settings.sportradar_api_key:
            self.scheduler.add_job(
                self._sportradar_job,
                "interval",
                seconds=settings.sportradar_poll_interval_seconds,
                id="sportradar",
                max_instances=1,
                next_run_time=datetime.now(timezone.utc),
            )
        self.scheduler.add_job(
            self._self_ping_job,
            "interval",
            minutes=5,
            id="self_ping",
            max_instances=1,
        )
        self.scheduler.add_job(
            self._scalp_job,
            "interval",
            seconds=60,
            id="scalp_alerts",
            max_instances=1,
            next_run_time=datetime.now(timezone.utc),
        )
        self.scheduler.add_job(
            self._football_poll_job,
            "interval",
            seconds=60,
            id="football_poll",
            max_instances=1,
            next_run_time=datetime.now(timezone.utc),
        )
        if settings.api_sports_key:
            self.scheduler.add_job(
                self._api_sports_job,
                "interval",
                seconds=settings.api_sports_poll_interval_seconds,
                id="api_sports",
                max_instances=1,
                next_run_time=datetime.now(timezone.utc),
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
        api_sports_status = f"API-Sports: active ({settings.api_sports_poll_interval_seconds}s)" if settings.api_sports_key else "API-Sports: no key"
        await self.notifier.send_text(
            "🎾⚽ Tennis + Football monitor started.\n"
            f"Tennis: ESPN + Flashscore + {bets_api_status} + {api_sports_status} (every {settings.sofascore_poll_interval}s)\n"
            f"Football: ESPN all leagues (every 60s)\n"
            f"Min confidence: {settings.min_confidence}"
        )
        log.info("scheduler_started")

    def get_status(self) -> dict:
        return {"collector_enabled": dict(self.collector_enabled),
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
                "quota_remaining": self.odds_api.quota_remaining,
                "quota_used": self.odds_api.quota_used,
                "last_events_fetched": self.odds_api.last_events_fetched,
            },
            "bets_api": {
                "token_set": bool(settings.bets_api_token),
                "consecutive_failures": self.bets_api._consecutive_failures,
            },
            "api_sports": {
                "key_set": bool(settings.api_sports_key),
                "consecutive_failures": self.api_sports._consecutive_failures,
                "quota_remaining": self.api_sports.quota_remaining,
                "last_live": self.api_sports.last_live_count,
                "last_scheduled": self.api_sports.last_scheduled_count,
                "poll_interval_secs": settings.api_sports_poll_interval_seconds,
            },
            "sportradar": {
                "key_set": bool(settings.sportradar_api_key),
                "consecutive_failures": self.sportradar._consecutive_failures,
                "poll_interval_secs": settings.sportradar_poll_interval_seconds,
            },
            "football": {
                "live_matches": 0,  # filled by health.py via football_store.count()
                "signals_today": len(self.football_engine.get_recent_signals(24)),
                "odds_api_football": bool(settings.odds_api_key),
            },
        }

    async def stop(self) -> None:
        self.scheduler.shutdown(wait=False)
        await self.sofascore.close()
        await self.notifier.send_text("Tennis-bet monitor stopped.")
        log.info("scheduler_stopped")
