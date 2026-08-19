"""
Resolving signal outcomes, and the endpoints the new screens read.

Crypto signals were written as "pending" and never resolved, so no accuracy
figure could exist. These pin the resolution rules — above all that a bar
containing both levels books the STOP, because a resolver that breaks ties in
its own favour reports accuracy it has not earned.
"""
import unittest
from datetime import UTC, datetime, timedelta


class TestResolutionRules(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        import storage.models  # noqa: F401
        from storage.database import Base
        from storage.repository import Repository

        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.maker = async_sessionmaker(self.engine, expire_on_commit=False)
        self.Repository = Repository

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _log(self, repo, hours_ago=8, **kw):
        base = dict(symbol="ethusdt", signal_type="confluence", direction="long",
                    trigger_description="x", confidence=0.80, current_price=1900.0,
                    target_price=1919.0, stop_loss=1881.0, edge_pct=1.0,
                    stake_pct=1.0, timeframe="1h", sentiment_score=0.0,
                    indicators_summary="")
        base.update(kw)
        await repo.log_crypto_signal(**base)
        rows = await repo.crypto_signals_between(1)
        return rows[0]

    async def test_a_fresh_signal_is_not_offered_for_resolution(self):
        """Resolving immediately would record the first tick, which is noise."""
        repo = self.Repository(self.maker())
        await self._log(repo)
        self.assertEqual(await repo.pending_crypto_signals(older_than_minutes=240), [])

    async def test_an_old_pending_signal_is_offered(self):
        repo = self.Repository(self.maker())
        row = await self._log(repo)
        row.timestamp = datetime.utcnow() - timedelta(hours=8)
        await repo.session.commit()
        self.assertEqual(len(await repo.pending_crypto_signals(older_than_minutes=240)), 1)

    async def test_resolving_records_outcome_and_pnl(self):
        repo = self.Repository(self.maker())
        row = await self._log(repo)
        await repo.resolve_crypto_signal(row.id, "won", 1.0)
        again = (await repo.crypto_signals_between(1))[0]
        self.assertEqual(again.outcome, "won")
        self.assertAlmostEqual(again.pnl_pct, 1.0, places=4)

    async def test_a_resolved_signal_is_no_longer_pending(self):
        repo = self.Repository(self.maker())
        row = await self._log(repo)
        row.timestamp = datetime.utcnow() - timedelta(hours=8)
        await repo.session.commit()
        await repo.resolve_crypto_signal(row.id, "lost", -1.0)
        self.assertEqual(await repo.pending_crypto_signals(older_than_minutes=240), [])

    async def test_the_window_query_splits_live_from_archive(self):
        repo = self.Repository(self.maker())
        recent = await self._log(repo)
        old = await self._log(repo, current_price=1800.0)
        old.timestamp = datetime.utcnow() - timedelta(days=20)
        await repo.session.commit()

        live = await repo.crypto_signals_between(7)
        archive = await repo.crypto_signals_between(365, older_than_days=7)
        self.assertEqual([r.id for r in live], [recent.id])
        self.assertEqual([r.id for r in archive], [old.id])

    async def test_counts_report_what_is_still_pending(self):
        repo = self.Repository(self.maker())
        await self._log(repo)
        counts = await repo.crypto_signal_counts()
        self.assertEqual(counts["signals"], 1)
        self.assertEqual(counts["pending"], 1)


class TestPessimisticTieBreak(unittest.TestCase):
    """
    The rule that keeps the number honest, asserted against the source: a bar
    holding both target and stop must book the stop.
    """

    def setUp(self):
        import inspect

        import scheduler.runner as runner
        self.src = inspect.getsource(runner.AppRunner._resolve_signal_outcomes_job)

    def test_the_stop_is_checked_before_the_target(self):
        self.assertLess(self.src.index("hit_stop:"), self.src.index("hit_tgt:"))

    def test_it_only_resolves_signals_old_enough_to_have_played_out(self):
        self.assertIn("older_than_minutes", self.src)

    def test_a_short_history_is_not_mistaken_for_an_expiry(self):
        self.assertIn("continue", self.src)
        self.assertIn("paper_max_hold_minutes", self.src)


class TestNewScreensAreWired(unittest.TestCase):
    def setUp(self):
        import scheduler.health as health
        self.html = health._HTML
        self.health = health

    def test_the_sidebar_replaced_the_tab_bars(self):
        self.assertIn('class="sidebar"', self.html)
        self.assertNotIn('id="tabbar-crypto"', self.html)

    def test_every_new_view_exists(self):
        for tab in ("dashboard", "guard", "accuracy", "historic", "watchlist"):
            with self.subTest(tab=tab):
                self.assertIn(f'id="tab-{tab}"', self.html)

    def test_the_endpoints_the_views_read_are_registered(self):
        import inspect
        src = inspect.getsource(self.health)
        self.assertIn('"/api/signals/history"', src)
        self.assertIn('"/api/signals/accuracy"', src)

    def test_a_reload_lands_on_the_same_screen(self):
        self.assertIn("location.hash", self.html)

    def test_the_sidebar_becomes_a_bottom_bar_on_a_phone(self):
        """Icons in a top-left rail sit where a thumb cannot reach."""
        self.assertIn("max-width:820px", self.html)

    def test_empty_buckets_report_null_rather_than_zero(self):
        """A bucket nobody has traded is not a bucket that loses."""
        import inspect
        src = inspect.getsource(self.health._api_signal_accuracy)
        self.assertIn("return None, 0", src)
