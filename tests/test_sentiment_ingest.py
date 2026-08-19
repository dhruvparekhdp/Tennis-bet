"""
Accepting scored headlines from an external analyser.

The analyser runs on a laptop behind NAT, so it pushes rather than being
polled. That makes two things load-bearing: the endpoint must be closed by
default, and a retried batch must not double-count a story.
"""
import unittest
from datetime import UTC, datetime, timedelta


class TestIngest(unittest.IsolatedAsyncioTestCase):
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

    def item(self, ext="a1", **kw):
        base = dict(external_id=ext, symbol="xauusdt", headline="Gold rallies on Fed hold",
                    score=0.6, confidence=0.8, event_type="fed", source="Reuters",
                    published_at=datetime.now(UTC).isoformat(), model="finbert")
        base.update(kw)
        return base

    async def test_a_batch_is_stored(self):
        repo = self.Repository(self.maker())
        accepted, dupes = await repo.ingest_news_sentiment([self.item("a1"), self.item("a2")])
        self.assertEqual((accepted, dupes), (2, 0))

    async def test_a_retried_batch_does_not_double_count(self):
        """
        The failure this prevents: a retry after a timeout would otherwise
        manufacture a sentiment spike that never happened.
        """
        repo = self.Repository(self.maker())
        await repo.ingest_news_sentiment([self.item("a1")])
        accepted, dupes = await repo.ingest_news_sentiment([self.item("a1")])
        self.assertEqual((accepted, dupes), (0, 1))

    async def test_scores_are_clamped_to_the_documented_range(self):
        repo = self.Repository(self.maker())
        await repo.ingest_news_sentiment([self.item("hi", score=9.0),
                                          self.item("lo", score=-9.0)])
        rows = await repo.recent_news_sentiment("xauusdt", hours=6)
        scores = sorted(r.score for r in rows)
        self.assertEqual(scores, [-1.0, 1.0])

    async def test_an_item_without_an_id_is_skipped_not_stored(self):
        repo = self.Repository(self.maker())
        accepted, _ = await repo.ingest_news_sentiment([{"symbol": "x", "score": 0.5}])
        self.assertEqual(accepted, 0)

    async def test_a_bad_timestamp_falls_back_instead_of_failing_the_batch(self):
        """One malformed field must not cost the other 499 items."""
        repo = self.Repository(self.maker())
        accepted, _ = await repo.ingest_news_sentiment(
            [self.item("t1", published_at="not-a-date"), self.item("t2")])
        self.assertEqual(accepted, 2)

    async def test_market_wide_items_reach_every_symbol(self):
        repo = self.Repository(self.maker())
        await repo.ingest_news_sentiment([self.item("g1", symbol="ALL")])
        for sym in ("xauusdt", "ethusdt"):
            with self.subTest(sym=sym):
                self.assertEqual(len(await repo.recent_news_sentiment(sym)), 1)

    async def test_another_symbol_does_not_leak_in(self):
        repo = self.Repository(self.maker())
        await repo.ingest_news_sentiment([self.item("e1", symbol="ethusdt")])
        self.assertEqual(await repo.recent_news_sentiment("xauusdt"), [])

    async def test_stale_items_fall_out_of_the_window(self):
        repo = self.Repository(self.maker())
        old = (datetime.now(UTC) - timedelta(hours=20)).isoformat()
        await repo.ingest_news_sentiment([self.item("old", published_at=old)])
        self.assertEqual(len(await repo.recent_news_sentiment("xauusdt", hours=6)), 0)
        self.assertEqual(len(await repo.recent_news_sentiment("xauusdt", hours=24)), 1)


class TestEndpointIsClosedByDefault(unittest.TestCase):
    def test_no_token_configured_means_the_endpoint_is_off(self):
        """An ingest endpoint that defaults to open is an open write endpoint."""
        from config.settings import Settings
        self.assertEqual(Settings().sentiment_ingest_token, "")

    def test_the_handler_compares_the_token_in_constant_time(self):
        """A plain == leaks the secret one character at a time to a timer."""
        import inspect

        import scheduler.health as health
        src = inspect.getsource(health._api_sentiment_ingest)
        self.assertIn("compare_digest", src)
        self.assertNotIn("supplied == secret", src)

    def test_batches_are_bounded(self):
        import inspect

        import scheduler.health as health
        self.assertIn("500", inspect.getsource(health._api_sentiment_ingest))
