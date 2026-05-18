"""
Collector for The Odds API (https://api.the-odds-api.com) v4.

Key facts from docs:
  - /v4/sports/          free, doesn't count against quota → call every poll
  - /v4/sports/{s}/odds/ costs 1 request per sport per call
  - commence_time < now  → event is live/in-play
  - commenceTimeFrom/To  → filter to today's window to avoid future events
  - regions=eu           → European bookmakers (Bet365, Pinnacle, etc.)
  - tennis_atp / tennis_wta are valid generic keys when in-season;
    tournament-specific keys (tennis_atp_french_open) also appear
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import structlog

from analysis.match_state import MatchState, OddsPoint, ServeStats
from analysis.state_store import MatchStateStore
from config.settings import settings
from storage.database import AsyncSessionFactory
from storage.repository import Repository

log = structlog.get_logger()

_API_BASE = "https://api.the-odds-api.com/v4"


def _last_name(full_name: str) -> str:
    return full_name.strip().split()[-1].lower() if full_name.strip() else ""


def _best_odds(bookmakers: list[dict], idx: int) -> float:
    """Lowest decimal back price for outcome at position idx across all bookmakers."""
    best: float = 0.0
    for bm in bookmakers:
        for market in bm.get("markets", []):
            if market.get("key") != "h2h":
                continue
            outcomes = market.get("outcomes", [])
            if len(outcomes) > idx:
                price = float(outcomes[idx].get("price", 0.0))
                if price > 1.0 and (best == 0.0 or price < best):
                    best = price
    return best


def _sport_key_to_meta(sport_key: str) -> tuple[str, str]:
    """Return (tournament_name, surface) from a sport key like 'tennis_atp_italian_open'."""
    # Strip tour prefix
    name = sport_key.replace("tennis_atp_", "").replace("tennis_wta_", "")
    name = name.replace("_", " ").title()

    # Surface heuristics
    clay_keywords = ("clay", "french_open", "roland", "madrid", "rome", "italian",
                     "barcelona", "monte", "hamburg", "lyon", "geneva")
    grass_keywords = ("grass", "wimbledon", "queens", "halle", "eastbourne", "birmingham")
    surface = "hard"
    for kw in clay_keywords:
        if kw in sport_key.lower():
            surface = "clay"
            break
    for kw in grass_keywords:
        if kw in sport_key.lower():
            surface = "grass"
            break

    return name, surface


class OddsApiCollector:
    """Fetches odds from The Odds API and updates the in-memory MatchStateStore."""

    def __init__(self, store: MatchStateStore) -> None:
        self.store = store
        # Track which odds_* match IDs we created so we can remove finished ones
        self._odds_live_ids: set[str] = set()

    async def _get_active_tennis_sports(self, client: httpx.AsyncClient, api_key: str) -> list[str]:
        """
        GET /v4/sports/ — free, doesn't count against quota.
        Returns all currently active tennis sport keys.
        """
        try:
            resp = await client.get(f"{_API_BASE}/sports/", params={"apiKey": api_key})
            if resp.status_code != 200:
                log.error("odds_api_sports_error", status=resp.status_code, body=resp.text[:300])
                return []
            all_sports: list[dict] = resp.json()
            keys = [
                s["key"] for s in all_sports
                if "tennis" in s.get("key", "").lower() and s.get("active", False)
            ]
            log.info("odds_api_sports_fetched", active_tennis=keys)
            return keys
        except Exception as exc:
            log.error("odds_api_sports_fetch_failed", error=str(exc))
            return []

    async def fetch(self) -> None:
        api_key = settings.odds_api_key
        if not api_key:
            log.warning("odds_api_key_missing", hint="Set ODDS_API_KEY in environment")
            return

        now = datetime.now(timezone.utc)
        commence_time_to = (now + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # 12h lookback — tennis matches can run 3-4h; qualifiers start early
        commence_time_from = (now - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")

        total_updated = 0
        total_fetched = 0
        quota_remaining: str | None = None
        current_live_ids: set[str] = set()

        async with httpx.AsyncClient(timeout=15.0) as client:
            sports = await self._get_active_tennis_sports(client, api_key)
            if not sports:
                log.warning("odds_api_no_active_tennis")
                return

            for sport in sports:
                try:
                    resp = await client.get(
                        f"{_API_BASE}/sports/{sport}/odds/",
                        params={
                            "apiKey": api_key,
                            "regions": "eu",
                            "markets": "h2h",
                            "oddsFormat": "decimal",
                            "commenceTimeFrom": commence_time_from,
                            "commenceTimeTo": commence_time_to,
                        },
                    )
                except httpx.HTTPError as exc:
                    log.error("odds_api_http_error", sport=sport, error=str(exc))
                    continue

                if resp.status_code != 200:
                    log.error("odds_api_bad_status", sport=sport,
                              status_code=resp.status_code, body=resp.text[:300])
                    continue

                quota_remaining = resp.headers.get("X-Requests-Remaining", quota_remaining)
                quota_used = resp.headers.get("X-Requests-Used", "?")

                try:
                    events: list[dict] = resp.json()
                except Exception as exc:
                    log.error("odds_api_json_error", sport=sport, error=str(exc))
                    continue

                total_fetched += len(events)

                for event in events:
                    home = event.get("home_team", "?")
                    away = event.get("away_team", "?")
                    ct_str = event.get("commence_time", "")
                    is_live = False
                    try:
                        ct = datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
                        mins_until = int((ct - now).total_seconds() / 60)
                        if mins_until <= 0:
                            status = f"LIVE ({-mins_until}m ago)"
                            is_live = True
                        else:
                            status = f"starts_in_{mins_until}m"
                    except Exception:
                        status = "unknown"

                    log.info(
                        "odds_api_event",
                        sport=sport,
                        match=f"{home} vs {away}",
                        status=status,
                        bookmakers=len(event.get("bookmakers", [])),
                    )

                    if is_live:
                        try:
                            match_id = await self._process_event(event, sport, now)
                            if match_id:
                                current_live_ids.add(match_id)
                                total_updated += 1
                        except Exception as exc:
                            log.exception("odds_api_event_error",
                                          event_id=event.get("id"), error=str(exc))

        # Remove odds-created states that are no longer live
        for stale_id in self._odds_live_ids - current_live_ids:
            existing = await self.store.get(stale_id)
            if existing:
                await self.store.remove(stale_id)
                log.info("odds_api_match_removed", match_id=stale_id)
        self._odds_live_ids = current_live_ids

        log.info(
            "odds_api_done",
            matches_updated=total_updated,
            events_fetched=total_fetched,
            quota_remaining=quota_remaining,
            quota_used=quota_used,
        )

    async def _process_event(self, event: dict, sport_key: str, now: datetime) -> str | None:
        """
        Update odds on an existing MatchState, or create a new one if the match is
        live but not yet tracked by ESPN/Flashscore.
        Returns the match_id that was updated/created, or None.
        """
        home_team: str = event.get("home_team", "")
        away_team: str = event.get("away_team", "")
        bookmakers: list[dict] = event.get("bookmakers", [])

        if not home_team or not away_team or not bookmakers:
            return None

        # Skip doubles — Odds API represents doubles teams as "Player1 / Player2"
        if "/" in home_team or "/" in away_team:
            log.info("odds_api_skipped_doubles", home=home_team[:40], away=away_team[:40])
            return None

        odds_home = _best_odds(bookmakers, 0)
        odds_away = _best_odds(bookmakers, 1)
        if odds_home <= 1.0 or odds_away <= 1.0:
            return None

        home_last = _last_name(home_team)
        away_last = _last_name(away_team)

        # Try to match against an existing state (from ESPN/Flashscore)
        states = await self.store.get_all()
        existing: MatchState | None = None
        reversed_order = False

        for state in states:
            p1_last = _last_name(state.player1_name)
            p2_last = _last_name(state.player2_name)
            if p1_last == home_last and p2_last == away_last:
                existing = state
                break
            if p1_last == away_last and p2_last == home_last:
                existing = state
                reversed_order = True
                break

        if existing is not None:
            # Enrich existing state with live odds
            new_p1 = odds_away if reversed_order else odds_home
            new_p2 = odds_home if reversed_order else odds_away
            existing.odds_p1 = new_p1
            existing.odds_p2 = new_p2
            existing.odds_history.append(
                OddsPoint(odds_p1=new_p1, odds_p2=new_p2, timestamp=datetime.utcnow())
            )
            _save_snapshot(existing.match_id, new_p1, new_p2)
            log.info("odds_api_updated", match_id=existing.match_id,
                     player1=existing.player1_name, odds_p1=new_p1,
                     player2=existing.player2_name, odds_p2=new_p2)
            return existing.match_id

        # No existing state — create one directly from odds data so the dashboard shows it
        odds_id = f"odds_{event.get('id', home_last + '_' + away_last)}"

        # Check if we already have an odds-created state for this match
        existing_odds = await self.store.get(odds_id)
        tournament, surface = _sport_key_to_meta(sport_key)

        if existing_odds is not None:
            # Update odds on the existing odds-created state
            existing_odds.odds_p1 = odds_home
            existing_odds.odds_p2 = odds_away
            existing_odds.odds_history.append(
                OddsPoint(odds_p1=odds_home, odds_p2=odds_away, timestamp=datetime.utcnow())
            )
            _save_snapshot(odds_id, odds_home, odds_away)
            log.info("odds_api_odds_state_updated", match_id=odds_id,
                     player1=home_team, odds_p1=odds_home,
                     player2=away_team, odds_p2=odds_away)
            return odds_id

        # Create brand-new MatchState from odds data
        new_state = MatchState(
            match_id=odds_id,
            player1_name=home_team,
            player2_name=away_team,
            surface=surface,
            tournament=tournament,
            current_server=0,
            sets_p1=0,
            sets_p2=0,
            games_in_set_p1=0,
            games_in_set_p2=0,
            current_set=1,
            is_tiebreak=False,
            serve_stats_p1=ServeStats(),
            serve_stats_p2=ServeStats(),
            odds_p1=odds_home,
            odds_p2=odds_away,
            odds_history=[OddsPoint(odds_p1=odds_home, odds_p2=odds_away,
                                    timestamp=datetime.utcnow())],
            game_log=[],
            match_duration_mins=0,
            timestamp=datetime.utcnow(),
        )
        await self.store.update(new_state)
        _save_snapshot(odds_id, odds_home, odds_away)
        log.info("odds_api_match_created", match_id=odds_id,
                 player1=home_team, odds_p1=odds_home,
                 player2=away_team, odds_p2=odds_away,
                 tournament=tournament, surface=surface)
        return odds_id


def _save_snapshot(match_id: str, p1: float, p2: float) -> None:
    """Fire-and-forget DB write — errors are logged but don't block the caller."""
    import asyncio

    async def _write() -> None:
        try:
            async with AsyncSessionFactory() as session:
                repo = Repository(session)
                await repo.save_odds_snapshot(match_id, p1, p2)
        except Exception as exc:
            log.error("odds_api_db_error", match_id=match_id, error=str(exc))

    try:
        loop = asyncio.get_event_loop()
        loop.create_task(_write())
    except RuntimeError:
        pass
