"""
ESPN public soccer scoreboard collector.

Covers all major leagues worldwide — no API key needed.
Updates every ~30 seconds.

Limitations:
- No live odds (use Odds API for that)
- Red card detection via competition details (may not always be populated)
- Match minute derived from displayClock / period
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import structlog

from analysis.football_state import FootballMatchState, FootballStateStore

log = structlog.get_logger()

# All leagues ESPN exposes via the same scoreboard endpoint pattern
_LEAGUES = [
    # Top 5 European
    "eng.1",                    # Premier League
    "esp.1",                    # La Liga
    "ger.1",                    # Bundesliga
    "ita.1",                    # Serie A
    "fra.1",                    # Ligue 1
    # European cups
    "uefa.champions_league",
    "uefa.europa",
    "uefa.europa.conf",         # Conference League
    # Other top leagues
    "ned.1",                    # Eredivisie
    "por.1",                    # Primeira Liga
    "tur.1",                    # Süper Lig
    "sco.1",                    # Scottish Premiership
    "bel.1",                    # Jupiler Pro League
    "rus.1",                    # Russian Premier League
    # Americas
    "usa.1",                    # MLS
    "mex.1",                    # Liga MX
    "bra.1",                    # Brasileirão
    "arg.1",                    # Primera División Argentina
    "col.1",                    # Liga Betplay
    # Second divisions
    "eng.2",                    # Championship
    "ger.2",                    # 2. Bundesliga
    "esp.2",                    # Segunda División
    "ita.2",                    # Serie B
    # International
    "fifa.world",               # World Cup
    "uefa.nations",             # Nations League
    "conmebol.america",         # Copa América
]

_LIVE_STATUSES = {
    "STATUS_IN_PROGRESS", "STATUS_LIVE", "STATUS_PLAY",
    "STATUS_HALFTIME", "IN_PROGRESS", "LIVE", "PLAYING",
}

_HALFTIME_STATUSES = {"STATUS_HALFTIME", "HALFTIME"}


def _parse_minute(status: dict) -> int:
    """Extract elapsed match minute from ESPN status."""
    period = status.get("period", 1)

    # displayClock format: "27.0", "45+2.0", "72.0", "45'"
    display = status.get("displayClock", "")
    if display:
        try:
            clean = display.replace("'", "").split("+")[0].split(".")[0].strip()
            base = int(clean)
            # ESPN resets clock for 2nd half — offset
            if period == 2 and base < 45:
                base = 45 + base
            elif period >= 3:
                base = 90 + base
            return max(0, min(120, base))
        except (ValueError, IndexError):
            pass

    # Fallback: clock in seconds elapsed
    clock = status.get("clock", 0)
    try:
        elapsed_s = float(clock)
        base = int(elapsed_s / 60)
        if period == 2:
            base = 45 + base
        elif period >= 3:
            base = 90 + base
        return max(0, min(120, base))
    except (TypeError, ValueError):
        return 45 if period == 2 else 0


def _count_red_cards(details: list[dict], team_id: str) -> int:
    count = 0
    for d in details:
        dtype = ((d.get("type") or {}).get("text") or "").lower()
        if "red" in dtype or "violent" in dtype:
            if ((d.get("team") or {}).get("id") or "") == team_id:
                count += 1
    return count


class FootballESPNCollector:
    """ESPN public soccer scoreboard — all available leagues, no auth required."""

    def __init__(self, store: FootballStateStore) -> None:
        self.store = store

    async def fetch(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        live_ids: set[str] = set()
        total_fetched = 0

        async with httpx.AsyncClient(timeout=15.0) as client:
            for league in _LEAGUES:
                url = (
                    f"https://site.api.espn.com/apis/site/v2/sports/soccer"
                    f"/{league}/scoreboard"
                )
                try:
                    resp = await client.get(url, params={"dates": today, "limit": "100"})
                    if resp.status_code >= 400:
                        continue  # silently skip 404/403/400/etc
                    data = resp.json()
                    events = data.get("events", [])
                    total_fetched += len(events)
                    for ev in events:
                        ev["_league_key"] = league
                        try:
                            state = self._parse_event(ev)
                            if state:
                                await self.store.update(state)
                                live_ids.add(state.match_id)
                        except Exception:
                            log.exception("football_espn_parse_failed",
                                          event_id=ev.get("id"), league=league)
                except Exception:
                    log.exception("football_espn_fetch_failed", league=league)

        # Remove matches that disappeared from the live feed
        for state in await self.store.get_all():
            if state.match_id.startswith("fb_") and state.match_id not in live_ids:
                await self.store.remove(state.match_id)

        log.info("football_espn_done",
                 total_events=total_fetched, live_matches=len(live_ids))

    def _parse_event(self, event: dict) -> FootballMatchState | None:
        status_obj = event.get("status", {})
        type_obj = status_obj.get("type") or {}
        status_name = type_obj.get("name", "")

        if status_name not in _LIVE_STATUSES:
            log.debug("football_event_skipped",
                      event_id=event.get("id"),
                      status=status_name,
                      league=event.get("_league_key", ""))
            return None

        is_halftime = status_name in _HALFTIME_STATUSES
        period = status_obj.get("period", 1)
        is_extra_time = period > 2
        minute = _parse_minute(status_obj)

        match_id = f"fb_{event.get('id', '')}"
        competitions = event.get("competitions", [])
        if not competitions:
            return None

        comp = competitions[0]
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            return None

        home_comp = next(
            (c for c in competitors if c.get("homeAway") == "home"), competitors[0]
        )
        away_comp = next(
            (c for c in competitors if c.get("homeAway") == "away"), competitors[1]
        )

        def _name(c: dict) -> str:
            return (
                (c.get("team") or {}).get("displayName")
                or c.get("displayName")
                or "Unknown"
            )

        home_team = _name(home_comp)
        away_team = _name(away_comp)
        home_score = int(home_comp.get("score", "0") or 0)
        away_score = int(away_comp.get("score", "0") or 0)

        details = comp.get("details", [])
        home_id = (home_comp.get("team") or {}).get("id", "")
        away_id = (away_comp.get("team") or {}).get("id", "")
        home_red = _count_red_cards(details, home_id)
        away_red = _count_red_cards(details, away_id)

        # Use event name if available, otherwise league key
        tournament = (
            event.get("name")
            or (comp.get("tournament") or {}).get("displayName")
            or event.get("_league_key", "Unknown")
        )
        league_key = event.get("_league_key", "")

        return FootballMatchState(
            match_id=match_id,
            home_team=home_team,
            away_team=away_team,
            tournament=tournament,
            league_key=league_key,
            minute=minute,
            home_score=home_score,
            away_score=away_score,
            home_red_cards=home_red,
            away_red_cards=away_red,
            is_halftime=is_halftime,
            is_extra_time=is_extra_time,
            period=period,
            timestamp=datetime.utcnow(),
        )
