"""
Collector for The Odds API (https://api.the-odds-api.com).

Fetches pre-match / in-play odds for ATP and WTA tennis, matches events to
live MatchState entries by player last-name, updates odds in-memory and
persists an OddsSnapshot to the database.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import structlog

from analysis.match_state import MatchState, OddsPoint
from analysis.state_store import MatchStateStore
from config.settings import settings
from storage.database import AsyncSessionFactory
from storage.repository import Repository

log = structlog.get_logger()

_BASE_URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds/"
_SPORTS = ["tennis_atp", "tennis_wta"]


def _last_name(full_name: str) -> str:
    """Return the last word of a name, lower-cased."""
    return full_name.strip().split()[-1].lower() if full_name.strip() else ""


def _best_odds(bookmakers: list[dict], home: bool) -> float:
    """Return the lowest (best for backer) decimal odds across all bookmakers.

    ``home=True`` → first outcome, ``home=False`` → second outcome.
    Returns 0.0 if nothing found.
    """
    best: float = 0.0
    idx = 0 if home else 1
    for bm in bookmakers:
        for market in bm.get("markets", []):
            if market.get("key") != "h2h":
                continue
            outcomes = market.get("outcomes", [])
            if len(outcomes) > idx:
                price: float = outcomes[idx].get("price", 0.0)
                if price > 1.0:
                    # lowest odds = best for backer from a backing perspective
                    if best == 0.0 or price < best:
                        best = price
    return best


class OddsApiCollector:
    """Fetches odds from The Odds API and updates the in-memory MatchStateStore."""

    def __init__(self, store: MatchStateStore) -> None:
        self.store = store

    async def fetch(self) -> None:
        api_key = settings.odds_api_key
        if not api_key:
            log.warning("odds_api_key_missing", msg="ODDS_API_KEY not set — skipping odds fetch")
            return

        total_updated = 0
        total_fetched = 0
        quota_remaining: str | None = None

        async with httpx.AsyncClient(timeout=15.0) as client:
            for sport in _SPORTS:
                url = _BASE_URL.format(sport=sport)
                params = {
                    "apiKey": api_key,
                    "regions": "eu",
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                }
                try:
                    resp = await client.get(url, params=params)
                except httpx.HTTPError as exc:
                    log.error("odds_api_http_error", sport=sport, error=str(exc))
                    continue

                if resp.status_code != 200:
                    log.error(
                        "odds_api_bad_status",
                        sport=sport,
                        status_code=resp.status_code,
                        body=resp.text[:200],
                    )
                    continue

                quota_remaining = resp.headers.get("X-Requests-Remaining", quota_remaining)

                try:
                    events: list[dict] = resp.json()
                except Exception as exc:
                    log.error("odds_api_json_error", sport=sport, error=str(exc))
                    continue

                total_fetched += len(events)

                # Log every event returned so we can see what the API has
                now = datetime.now(timezone.utc)
                for event in events:
                    home = event.get("home_team", "?")
                    away = event.get("away_team", "?")
                    ct_str = event.get("commence_time", "")
                    try:
                        ct = datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
                        mins_until = int((ct - now).total_seconds() / 60)
                        status = "LIVE" if mins_until <= 0 else f"starts in {mins_until}m"
                    except Exception:
                        status = "unknown time"
                    bm_count = len(event.get("bookmakers", []))
                    log.info(
                        "odds_api_event",
                        sport=sport,
                        match=f"{home} vs {away}",
                        status=status,
                        bookmakers=bm_count,
                    )

                for event in events:
                    try:
                        updated = await self._process_event(event)
                        if updated:
                            total_updated += 1
                    except Exception as exc:
                        log.exception("odds_api_event_error", event_id=event.get("id"), error=str(exc))

        log.info(
            "odds_api_done",
            matches_updated=total_updated,
            events_fetched=total_fetched,
            quota_remaining=quota_remaining,
        )

    async def _process_event(self, event: dict) -> bool:
        """Match event to MatchState and update odds. Returns True if matched."""
        home_team: str = event.get("home_team", "")
        away_team: str = event.get("away_team", "")
        bookmakers: list[dict] = event.get("bookmakers", [])

        if not home_team or not away_team or not bookmakers:
            return False

        odds_home = _best_odds(bookmakers, home=True)
        odds_away = _best_odds(bookmakers, home=False)

        if odds_home <= 1.0 or odds_away <= 1.0:
            return False

        # Try to find a matching MatchState by last-name
        states = await self.store.get_all()
        match: MatchState | None = None
        reversed_order = False

        home_last = _last_name(home_team)
        away_last = _last_name(away_team)

        for state in states:
            p1_last = _last_name(state.player1_name)
            p2_last = _last_name(state.player2_name)

            if p1_last == home_last and p2_last == away_last:
                match = state
                reversed_order = False
                break
            if p1_last == away_last and p2_last == home_last:
                match = state
                reversed_order = True
                break

        if match is None:
            return False

        # Assign odds respecting player order
        if reversed_order:
            new_p1 = odds_away
            new_p2 = odds_home
        else:
            new_p1 = odds_home
            new_p2 = odds_away

        # Update in-memory state
        match.odds_p1 = new_p1
        match.odds_p2 = new_p2
        match.odds_history.append(
            OddsPoint(odds_p1=new_p1, odds_p2=new_p2, timestamp=datetime.utcnow())
        )

        # Persist snapshot
        try:
            async with AsyncSessionFactory() as session:
                repo = Repository(session)
                await repo.save_odds_snapshot(match.match_id, new_p1, new_p2)
        except Exception as exc:
            log.error("odds_api_db_error", match_id=match.match_id, error=str(exc))

        log.info(
            "odds_api_updated",
            match_id=match.match_id,
            odds_p1=new_p1,
            odds_p2=new_p2,
        )
        return True
