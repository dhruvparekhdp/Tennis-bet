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
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from analysis.engine import AnalysisEngine
from analysis.state_store import MatchStateStore
from collectors.espn import ESPNCollector
from collectors.flashscore import FlashscoreCollector
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
        self.notifier = TelegramNotifier()
        self.scheduler = AsyncIOScheduler()

    async def _data_poll_job(self) -> None:
        # Flashscore: primary — covers ATP, WTA, Challengers, ITF
        await self.flashscore.fetch()

        # ESPN: supplement for ATP/WTA main draw when Flashscore is blocked
        if self.flashscore._consecutive_failures >= 5:
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
        log.info("heartbeat", matches_tracked=count, sofascore_available=sofascore_ok)

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

    async def start(self) -> None:
        # Verify Telegram credentials before starting — logs exact error if wrong
        await self.notifier.verify()

        self.setup_jobs()
        self.scheduler.start()
        await self.notifier.send_text(
            "🎾 Tennis-bet monitor started.\n"
            f"Polling every {settings.sofascore_poll_interval}s | "
            f"Min confidence: {settings.min_confidence} | "
            "Data: Flashscore → ESPN → Sofascore (serve stats)"
        )
        log.info("scheduler_started")

    async def stop(self) -> None:
        self.scheduler.shutdown(wait=False)
        await self.sofascore.close()
        await self.notifier.send_text("Tennis-bet monitor stopped.")
        log.info("scheduler_stopped")
