"""
APScheduler-based 24/7 job runner.

Data collection strategy:
  - ESPN (primary)     → always works from cloud IPs, covers ATP + WTA live scores
  - Sofascore (enrich) → attempted for serve stats only; silently skipped if blocked

Jobs:
  - data_poll:      every 30s  → ESPN fetch + optional Sofascore + analysis + snapshots
  - schedule_poll:  every 5min → TheSportsDB schedule
  - db_cleanup:     daily      → delete old odds/crypto/commodity snapshots
  - heartbeat:      every 10m  → log status

Data storage:
  - MatchSnapshot: saved every ~2 minutes per match (every 4 polls)
  - MatchCompletion: saved when a match disappears from the live feed
  - SignalLog: updated with outcome (won/lost) when match completes
"""
import json
import os
import asyncio
from datetime import datetime, timezone

import httpx
import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram.constants import ParseMode

from analysis.crypto_engine import CryptoEngine
from analysis.crypto_state_store import CommodityStateStore, CryptoStateStore
from analysis.engine import AnalysisEngine
from analysis.football_engine import FootballEngine
from analysis.football_state import FootballStateStore
from analysis.match_state import MatchState
from analysis.ml_predictor import MLPredictor
from analysis.multi_horizon_predictor import MultiHorizonPredictor
from analysis.scalping import scan_all
from analysis.sentiment import SentimentAnalyzer
from analysis.state_store import MatchStateStore
from analysis.win_probability import compute_win_probability
from collectors.api_tennis import ApiTennisCollector
from collectors.bets_api import BetsAPICollector
from collectors.binance_ws import BinanceWSCollector
from collectors.coindcx import CoinDCXCollector
from collectors.coingecko import CoinGeckoCollector
from collectors.cryptopanic import CryptoPanicCollector
from collectors.espn import ESPNCollector
from collectors.flashscore import FlashscoreCollector
from collectors.football_espn import FootballESPNCollector
from collectors.football_odds_api import FootballOddsApiCollector
from collectors.historical_importer import run_import
from collectors.odds_api import OddsApiCollector
from collectors.slam_pbp_importer import run_slam_import
from collectors.sofascore import SofascoreCollector
from collectors.sportradar import SportradarCollector
from collectors.sportsdata import SportsDataCollector
from collectors.thesportsdb import TheSportsDBCollector
from collectors.twelvedata_ws import TwelveDataWSCollector
from config.settings import settings
from notifications.crypto_formatter import format_crypto_signal
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
        # SportsData.io — live + scheduled tennis
        self.sportsdata = SportsDataCollector(self.store)
        # API-Tennis — live + scheduled, no quota limits
        self.api_tennis = ApiTennisCollector(self.store)
        # Scalping alerts — track last Telegram ping per match to avoid spam
        self._scalp_alert_times: dict[str, datetime] = {}
        # Crypto & Commodities — watchlist itself is DB-backed, loaded in start()
        self.crypto_store = CryptoStateStore()
        self.commodity_store = CommodityStateStore()
        # CoinDCX is the preferred crypto price source (free, no key, exact
        # exchange prices). CoinGecko fills in anything CoinDCX doesn't list.
        # Binance's WebSocket API returns HTTP 451 (geoblocked) from Render's
        # IPs, so it can't be relied on there — binance_ws is kept available
        # as an opt-in toggle (e.g. for a non-US deploy region) but starts
        # disabled.
        self.coindcx = CoinDCXCollector(self.crypto_store)
        self.coingecko = CoinGeckoCollector(self.crypto_store)
        self.binance_ws = BinanceWSCollector(self.crypto_store)
        self.twelvedata_ws = TwelveDataWSCollector(self.commodity_store)
        self.cryptopanic = CryptoPanicCollector()
        self.sentiment = SentimentAnalyzer()
        self.crypto_engine = CryptoEngine()
        self.multi_horizon = MultiHorizonPredictor()
        self._ws_tasks: list[asyncio.Task] = []
        # Collector enable/disable toggles (runtime, not persisted across restarts)
        self.collector_enabled: dict[str, bool] = {
            "sportradar": True,
            "sportsdata": True,
            "api_tennis": True,
            "odds_api": True,
            "api_sports": True,
            "espn": True,
            "bets_api": True,
            "coindcx": True,
            "coingecko": True,
            "binance_ws": False,  # geoblocked (HTTP 451) on Render — opt-in only
            "twelvedata_ws": True,
        }

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

    async def _sportradar_job(self) -> None:
        key = settings.sportradar_api_key
        if not key:
            return
        try:
            await self.sportradar.fetch_tennis(key)
        except Exception:
            log.exception("sportradar_tennis_job_failed")
        try:
            await self.sportradar.fetch_soccer(key)
        except Exception:
            log.exception("sportradar_soccer_job_failed")

    async def _sportsdata_job(self) -> None:
        if not settings.sportsdata_api_key:
            return
        if not self.collector_enabled.get("sportsdata", True):
            return
        try:
            await self.sportsdata.fetch()
        except Exception:
            log.exception("sportsdata_job_failed")

    async def _api_tennis_job(self) -> None:
        if not settings.api_tennis_key:
            return
        if not self.collector_enabled.get("api_tennis", True):
            return
        try:
            await self.api_tennis.fetch()
        except Exception:
            log.exception("api_tennis_job_failed")

    async def _coindcx_job(self) -> None:
        if not self.collector_enabled.get("coindcx", True):
            return
        try:
            await self.coindcx.fetch()
        except Exception:
            log.exception("coindcx_job_failed")

    async def _coingecko_job(self) -> None:
        if not self.collector_enabled.get("coingecko", True):
            return
        try:
            await self.coingecko.fetch()
        except Exception:
            log.exception("coingecko_job_failed")

    async def _cleanup_job(self) -> None:
        async with AsyncSessionFactory() as session:
            repo = Repository(session)
            await repo.delete_old_odds_snapshots(days=7)
            await repo.delete_old_crypto_data(days=7)
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

    async def _crypto_analysis_job(self) -> None:
        """Run crypto signal detection across all symbols in the watchlist."""
        try:
            states = await self.crypto_store.get_all()
            for state in states:
                if state.current_price <= 0:
                    continue

                signals = self.crypto_engine.process(state)
                for sig in signals:
                    msg = format_crypto_signal(sig)
                    if settings.crypto_alert_telegram:
                        await self.notifier.send_text(msg, parse_mode=ParseMode.HTML)

                    # Log to DB
                    try:
                        async with AsyncSessionFactory() as session:
                            repo = Repository(session)
                            await repo.log_crypto_signal(
                                symbol=sig.symbol,
                                signal_type=sig.signal_type,
                                direction=sig.direction,
                                trigger_description=sig.trigger_description,
                                confidence=sig.confidence,
                                current_price=sig.current_price,
                                target_price=sig.target_price,
                                stop_loss=sig.stop_loss,
                                edge_pct=sig.edge_pct,
                                stake_pct=sig.stake_pct,
                                timeframe=sig.timeframe,
                                sentiment_score=sig.sentiment_score,
                                indicators_summary=sig.indicators_summary,
                            )
                    except Exception:
                        log.exception("crypto_signal_db_log_failed", symbol=sig.symbol)

                    log.info(
                        "crypto_signal_fired",
                        symbol=sig.symbol,
                        type=sig.signal_type,
                        direction=sig.direction,
                        price=sig.current_price,
                        confidence=sig.confidence,
                    )
        except Exception:
            log.exception("crypto_analysis_job_failed")

    async def _crypto_news_job(self) -> None:
        """Poll CryptoPanic for news and compute global & coin-specific sentiment."""
        if not settings.cryptopanic_auth_token:
            return

        try:
            news_items = await self.cryptopanic.fetch()
            if not news_items:
                return

            headlines = [n.title for n in news_items]
            global_sentiment = self.sentiment.score(headlines)

            # Update overall sentiment
            await self.crypto_store.update_sentiment("ALL", global_sentiment, len(news_items))

            # Update coin-specific sentiment
            coin_headlines: dict[str, list[str]] = {}
            for item in news_items:
                for cur in item.currencies:
                    coin_headlines.setdefault(cur.upper(), []).append(item.title)

            for cur, titles in coin_headlines.items():
                cur_score = self.sentiment.score(titles)
                await self.crypto_store.update_sentiment(cur, cur_score, len(titles))

            log.info("crypto_news_sentiment_updated", global_score=global_sentiment, items_analyzed=len(news_items))
        except Exception:
            log.exception("crypto_news_job_failed")

    async def _crypto_snapshot_job(self) -> None:
        """Save periodic crypto & commodity snapshots for ML training and backtesting."""
        try:
            async with AsyncSessionFactory() as session:
                repo = Repository(session)
                # Crypto snapshots
                for state in await self.crypto_store.get_all():
                    if state.current_price > 0:
                        await repo.save_crypto_snapshot(
                            symbol=state.symbol,
                            price=state.current_price,
                            volume_24h=state.volume_24h,
                            rsi_14=state.rsi_14,
                            macd_line=state.macd_line,
                            macd_signal=state.macd_signal,
                            bollinger_upper=state.bollinger_upper,
                            bollinger_lower=state.bollinger_lower,
                            atr_14=state.atr_14,
                            sentiment_score=state.sentiment_score,
                        )

                # Commodity snapshots
                for cstate in await self.commodity_store.get_all():
                    if cstate.current_price > 0:
                        await repo.save_commodity_snapshot(
                            symbol=cstate.symbol,
                            price=cstate.current_price,
                            rsi_14=cstate.rsi_14,
                            atr_14=cstate.atr_14,
                        )
        except Exception:
            log.exception("crypto_snapshot_job_failed")

    # ── Crypto watchlist (DB-backed) ────────────────────────────────────────

    async def _load_crypto_watchlist(self) -> None:
        """Load the watchlist from the DB, seeding a small default the first
        time the table is empty (e.g. a brand-new deploy)."""
        try:
            async with AsyncSessionFactory() as session:
                repo = Repository(session)
                symbols = await repo.seed_crypto_watchlist_if_empty(
                    settings.crypto_watchlist_seed.split(",")
                )
            await self.crypto_store.seed(symbols)
            log.info("crypto_watchlist_loaded", count=len(symbols))
        except Exception:
            log.exception("crypto_watchlist_load_failed")

    async def add_crypto_symbol(self, symbol: str) -> None:
        sym = symbol.strip().lower()
        async with AsyncSessionFactory() as session:
            repo = Repository(session)
            await repo.add_crypto_watchlist_symbol(sym)
        await self.crypto_store.add_symbol(sym)

    async def remove_crypto_symbol(self, symbol: str) -> None:
        sym = symbol.strip().lower()
        async with AsyncSessionFactory() as session:
            repo = Repository(session)
            await repo.remove_crypto_watchlist_symbol(sym)
        await self.crypto_store.remove_symbol(sym)

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
        if settings.sportsdata_api_key:
            self.scheduler.add_job(
                self._sportsdata_job,
                "interval",
                seconds=settings.sportsdata_poll_interval_seconds,
                id="sportsdata",
                max_instances=1,
                next_run_time=datetime.now(timezone.utc),
            )
        if settings.api_tennis_key:
            self.scheduler.add_job(
                self._api_tennis_job,
                "interval",
                seconds=settings.api_tennis_poll_interval_seconds,
                id="api_tennis",
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
        # Crypto & Commodities Interval Jobs
        self.scheduler.add_job(
            self._coindcx_job,
            "interval",
            seconds=settings.coindcx_poll_interval_seconds,
            id="coindcx_poll",
            max_instances=1,
            next_run_time=datetime.now(timezone.utc),
        )
        self.scheduler.add_job(
            self._coingecko_job,
            "interval",
            seconds=settings.coingecko_poll_interval_seconds,
            id="coingecko_poll",
            max_instances=1,
            next_run_time=datetime.now(timezone.utc),
        )
        self.scheduler.add_job(
            self._crypto_analysis_job,
            "interval",
            seconds=60,
            id="crypto_analysis",
            max_instances=1,
            next_run_time=datetime.now(timezone.utc),
        )
        self.scheduler.add_job(
            self._crypto_news_job,
            "interval",
            seconds=settings.cryptopanic_poll_interval_seconds,
            id="crypto_news",
            max_instances=1,
            next_run_time=datetime.now(timezone.utc),
        )
        self.scheduler.add_job(
            self._crypto_snapshot_job,
            "interval",
            seconds=settings.crypto_snapshot_interval_seconds,
            id="crypto_snapshot",
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

        # Crypto watchlist lives in the DB — load/seed it before the WS collector
        # picks up symbols, so the very first connection already has the right set.
        await self._load_crypto_watchlist()

        self.setup_jobs()
        self.scheduler.start()

        # Launch continuous WebSocket tasks concurrently (non-blocking)
        if self.collector_enabled.get("binance_ws", True):
            task = asyncio.create_task(self.binance_ws.run_forever(), name="binance_ws")
            self._ws_tasks.append(task)
            log.info("binance_ws_task_spawned")

        if self.collector_enabled.get("twelvedata_ws", True) and settings.twelvedata_api_key:
            task = asyncio.create_task(self.twelvedata_ws.run_forever(), name="twelvedata_ws")
            self._ws_tasks.append(task)
            log.info("twelvedata_ws_task_spawned")

        bets_api_status = "BetsAPI: active" if settings.bets_api_token else "BetsAPI: no token"
        crypto_count = await self.crypto_store.count()
        await self.notifier.send_text(
            "🎾⚽🪙 Tennis + Football + Crypto monitor started.\n"
            f"Tennis: ESPN + Flashscore + {bets_api_status} (every {settings.sofascore_poll_interval}s)\n"
            f"Football: ESPN all leagues (every 60s)\n"
            f"Crypto: CoinDCX + CoinGecko {crypto_count}-symbol watchlist (poll every {settings.coindcx_poll_interval_seconds}s/{settings.coingecko_poll_interval_seconds}s, manage from Crypto tab)\n"
            f"Commodities: Twelve Data Gold/Silver/Oil ({'active' if settings.twelvedata_api_key else 'no key'})\n"
            f"News Sentiment: CryptoPanic ({'active' if settings.cryptopanic_auth_token else 'no token'})\n"
            f"Min confidence: {settings.min_confidence} (Sports) / {settings.crypto_min_confidence} (Crypto)"
        )
        log.info("scheduler_started", crypto_symbols_count=crypto_count)

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
                "quota_remaining": self.odds_api.quota_remaining,
                "quota_used": self.odds_api.quota_used,
                "last_events_fetched": self.odds_api.last_events_fetched,
            },
            "bets_api": {
                "token_set": bool(settings.bets_api_token),
                "consecutive_failures": self.bets_api._consecutive_failures,
            },
            "sportradar": {
                "key_set": bool(settings.sportradar_api_key),
                "consecutive_failures": self.sportradar._consecutive_failures,
                "poll_interval_secs": settings.sportradar_poll_interval_seconds,
            },
            "sportsdata": {
                "key_set": bool(settings.sportsdata_api_key),
                "consecutive_failures": self.sportsdata._consecutive_failures,
                "poll_interval_secs": settings.sportsdata_poll_interval_seconds,
                "quota_remaining": self.sportsdata.quota_remaining,
                "quota_total": self.sportsdata.quota_total,
                "last_live": self.sportsdata.last_live_count,
                "last_scheduled": self.sportsdata.last_scheduled_count,
            },
            "api_tennis": {
                "key_set": bool(settings.api_tennis_key),
                "consecutive_failures": self.api_tennis._consecutive_failures,
                "poll_interval_secs": settings.api_tennis_poll_interval_seconds,
                "last_live": self.api_tennis.last_live_count,
                "last_scheduled": self.api_tennis.last_scheduled_count,
            },
            "football": {
                "live_matches": 0,  # filled by health.py via football_store.count()
                "signals_today": len(self.football_engine.get_recent_signals(24)),
                "odds_api_football": bool(settings.odds_api_key),
            },
            "crypto": {
                "coindcx_consecutive_failures": self.coindcx._consecutive_failures,
                "coindcx_matched_symbols": len(self.coindcx.last_matched_symbols),
                "coingecko_consecutive_failures": self.coingecko._consecutive_failures,
                "binance_ws_connected": self.binance_ws._running and self.binance_ws._consecutive_failures == 0,
                "binance_messages_received": self.binance_ws._total_messages_received,
                "signals_today": len(self.crypto_engine.get_recent_signals(24)),
                "sentiment_mode": self.sentiment._mode,
                "cryptopanic_token": bool(settings.cryptopanic_auth_token),
                "twelvedata_key": bool(settings.twelvedata_api_key),
            },
        }

    async def stop(self) -> None:
        self.scheduler.shutdown(wait=False)
        self.binance_ws.stop()
        self.twelvedata_ws.stop()
        for task in self._ws_tasks:
            task.cancel()
        await self.sofascore.close()
        await self.notifier.send_text("Tennis + Football + Crypto monitor stopped.")
        log.info("scheduler_stopped")
