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

from analysis.match_state import MatchState, OddsPoint
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


class OddsApiCollector:
    """Fetches odds from The Odds API and updates the in-memory MatchStateStore."""

    def __init__(self, store: MatchStateStore) -> None:
        self.store = store

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
        # Fetch odds for matches starting up to 24h from now (covers today + tonight)
        commence_time_to = (now + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        commence_time_from = (now - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")

        total_updated = 0
        total_fetched = 0
        quota_remaining: str | None = None

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
                    try:
                        ct = datetime.fromisoformat(ct_str.replace("Z", "+00:00"))
                        mins_until = int((ct - now).total_seconds() / 60)
                        if mins_until <= 0:
                            status = f"LIVE ({-mins_until}m ago)"
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

                for event in events:
                    try:
                        if await self._process_event(event):
                            total_updated += 1
                    except Exception as exc:
                        log.exception("odds_api_event_error",
                                      event_id=event.get("id"), error=str(exc))

            log.info(
                "odds_api_done",
                matches_updated=total_updated,
                events_fetched=total_fetched,
                quota_remaining=quota_remaining,
                quota_used=quota_used,
            )

    async def _process_event(self, event: dict) -> bool:
        """Match event to a live MatchState by player last-name and update odds."""
        home_team: str = event.get("home_team", "")
        away_team: str = event.get("away_team", "")
        bookmakers: list[dict] = event.get("bookmakers", [])

        if not home_team or not away_team or not bookmakers:
            return False

        odds_home = _best_odds(bookmakers, 0)
        odds_away = _best_odds(bookmakers, 1)
        if odds_home <= 1.0 or odds_away <= 1.0:
            return False

        home_last = _last_name(home_team)
        away_last = _last_name(away_team)

        states = await self.store.get_all()
        match: MatchState | None = None
        reversed_order = False

        for state in states:
            p1_last = _last_name(state.player1_name)
            p2_last = _last_name(state.player2_name)
            if p1_last == home_last and p2_last == away_last:
                match = state
                break
            if p1_last == away_last and p2_last == home_last:
                match = state
                reversed_order = True
                break

        if match is None:
            return False

        new_p1 = odds_away if reversed_order else odds_home
        new_p2 = odds_home if reversed_order else odds_away

        match.odds_p1 = new_p1
        match.odds_p2 = new_p2
        match.odds_history.append(
            OddsPoint(odds_p1=new_p1, odds_p2=new_p2, timestamp=datetime.utcnow())
        )

        try:
            async with AsyncSessionFactory() as session:
                repo = Repository(session)
                await repo.save_odds_snapshot(match.match_id, new_p1, new_p2)
        except Exception as exc:
            log.error("odds_api_db_error", match_id=match.match_id, error=str(exc))

        log.info("odds_api_updated", match_id=match.match_id,
                 player1=match.player1_name, odds_p1=new_p1,
                 player2=match.player2_name, odds_p2=new_p2)
        return True
