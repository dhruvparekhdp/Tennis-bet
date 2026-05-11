"""
APScheduler-based 24/7 job runner.

Data strategy:
  - ESPN (primary)     → always works from cloud IPs, covers ATP + WTA live scores
  - Sofascore (enrich) → attempted for serve stats only; silently skipped if blocked

Jobs:
  - data_poll:      every 30s  → ESPN fetch + optional Sofascore enrichment + analysis
  - schedule_poll:  every 5min → TheSportsDB schedule
  - db_cleanup:     daily      → delete old odds snapshots
  - heartbeat:      every 10m  → log status
"""
import os
from datetime import datetime, timezone

import httpx
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from analysis.engine import AnalysisEngine
from analysis.ml_predictor import MLPredictor
from analysis.state_store import MatchStateStore
from collectors.espn import ESPNCollector
from collectors.flashscore import FlashscoreCollector
from collectors.odds_api import OddsApiCollector
from collectors.sofascore import SofascoreCollector
from collectors.thesportsdb import TheSportsDBCollector
from config.settings import settings
from notifications.telegram_notifier import TelegramNotifier
from storage.database import AsyncSessionFactory
from storage.repository import Repository

log = structlog.get_logger()


class AppRunner:
    def __init__(self) -> None:
        self.store = MatchStateStore()
        self.flashscore = FlashscoreCollector(self.store)
        self.espn = ESPNCollector(self.store)
        self.sofascore = SofascoreCollector(self.store)
        self.thesportsdb = TheSportsDBCollector(api_key=settings.thesportsdb_api_key)
        self.odds_api = OddsApiCollector(self.store)
        self.ml_predictor = MLPredictor()
        self.notifier = TelegramNotifier()
        self.scheduler = AsyncIOScheduler()

    async def _data_poll_job(self) -> None:
        # Flashscore: primary — covers ATP, WTA, Challengers, ITF
        await self.flashscore.fetch()

        # ESPN: always run — covers ATP/WTA main draw and acts as safety net
        # when Flashscore returns zero matches (format change / parsing issue)
        await self.espn.fetch()

        # Sofascore: serve stats enrichment only (blocked on most cloud IPs)
        if self.sofascore._consecutive_failures < 5:
            await self.sofascore.fetch()

        await self._run_analysis()

    async def _run_analysis(self) -> None:
        states = await self.store.get_all()
        if not states:
            return
        async with AsyncSessionFactory() as session:
            repo = Repository(session)
            engine = AnalysisEngine(repo)
            for state in states:
                try:
                    signals = await engine.process(state)
                    for sig in signals:
                        await self.notifier.send_signal(sig)
                        log.info(
                            "signal_fired",
                            signal_type=sig.signal_type,
                            match_id=sig.match_id,
                            player=sig.player_name,
                            confidence=sig.confidence,
                        )
                except Exception:
                    log.exception("analysis_job_failed", match_id=state.match_id)

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
        """Ping own /health endpoint to prevent Render free tier from spinning down."""
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
            next_run_time=datetime.now(timezone.utc),  # fire immediately on startup
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

    async def start(self) -> None:
        # Verify Telegram credentials before starting — logs exact error if wrong
        await self.notifier.verify()

        self.setup_jobs()
        self.scheduler.start()
        await self.notifier.send_text(
            "🎾 Tennis-bet monitor started.\n"
            f"Polling every {settings.sofascore_poll_interval}s | "
            f"Min confidence: {settings.min_confidence} | "
            "Data: Flashscore + ESPN (parallel) → Sofascore (serve stats)"
        )
        log.info("scheduler_started")

    def get_status(self) -> dict:
        from config.settings import settings
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
        }

    async def stop(self) -> None:
        self.scheduler.shutdown(wait=False)
        await self.sofascore.close()
        await self.notifier.send_text("Tennis-bet monitor stopped.")
        log.info("scheduler_stopped")
