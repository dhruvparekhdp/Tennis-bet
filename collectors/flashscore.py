"""
Flashscore internal feed collector.

Flashscore exposes a proprietary live feed at d.flashscore.in/x/feed/
used by their own website. Covers ATP, WTA, Challengers, ITF — far broader
than ESPN. Response uses a custom ¬/÷ text format.

Risk: may also be blocked from cloud IPs (same Cloudflare stack as Sofascore).
Provides immediate feedback in logs so we know within one poll.
"""
import asyncio
from datetime import datetime

import httpx
import structlog

from analysis.match_state import MatchState, OddsPoint, ServeStats
from analysis.state_store import MatchStateStore
from collectors.base import BaseCollector

log = structlog.get_logger()

# Flashscore internal feed endpoint — uses India domain since user is IN-based
_ENDPOINT = "https://d.flashscore.in/x/feed/f_1_5_0_en_1"

_HEADERS = {
    # X-Fsign is Flashscore's public auth token embedded in their JS bundle
    "X-GeoIP": "1",
    "X-Fsign": "SW9D1eZo",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.flashscore.in/tennis/",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Origin": "https://www.flashscore.in",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}

_SURFACE_MAP = {
    "clay": "clay",
    "hard": "hard",
    "grass": "grass",
    "indoor hard": "indoor_hard",
    "carpet": "indoor_hard",
}

# Flashscore status codes for tennis in-progress sets
_IN_PROGRESS_STATUSES = {"6", "7", "8", "9", "10", "31"}  # set 1–5 + tiebreak


def _parse_record(block: str) -> dict[str, str]:
    """Parse a ¬-delimited block of ÷-separated key-value pairs."""
    result: dict[str, str] = {}
    for field in block.split("¬"):
        if "÷" in field:
            key, _, val = field.partition("÷")
            result[key.strip()] = val.strip()
    return result


class FlashscoreCollector(BaseCollector):
    def __init__(self, store: MatchStateStore) -> None:
        self.store = store
        self._consecutive_failures = 0
        self._first_fetch = True

    async def fetch(self) -> None:
        if self._consecutive_failures >= 5:
            return  # silently stopped — blocked from this cloud IP

        try:
            async with httpx.AsyncClient(headers=_HEADERS, timeout=20.0,
                                         follow_redirects=True) as client:
                await asyncio.sleep(0.5)  # small jitter
                resp = await client.get(_ENDPOINT)
                resp.raise_for_status()
                raw = resp.text
        except httpx.HTTPStatusError as exc:
            self._consecutive_failures += 1
            if exc.response.status_code == 403:
                log.warning("flashscore_blocked_403",
                            consecutive_failures=self._consecutive_failures,
                            hint="Cloud IP blocked — will stop after 5 failures")
            else:
                log.error("flashscore_http_error", status=exc.response.status_code)
            return
        except Exception as exc:
            self._consecutive_failures += 1
            log.error("flashscore_fetch_failed", error=str(exc))
            return

        self._consecutive_failures = 0

        # Log a snippet of the raw format on first successful fetch for debugging
        if self._first_fetch:
            self._first_fetch = False
            log.info("flashscore_raw_sample", sample=raw[:300])

        live_ids: set[str] = set()
        matches_found = 0

        # Records are separated by ~ ; each record is a ¬÷ key-value block
        for block in raw.split("~"):
            record = _parse_record(block)
            if not record:
                continue

            match_id = record.get("AA", "")
            status = record.get("AE", "")

            if not match_id or status not in _IN_PROGRESS_STATUSES:
                continue

            matches_found += 1
            full_id = f"fs_{match_id}"
            state = self._build_state(full_id, record)
            if state:
                await self.store.update(state)
                live_ids.add(full_id)

        # Remove finished Flashscore matches from store
        for state in await self.store.get_all():
            if state.match_id.startswith("fs_") and state.match_id not in live_ids:
                await self.store.remove(state.match_id)

        log.info("flashscore_collector_done", live_matches=len(live_ids),
                 total_records=matches_found)

    def _build_state(self, match_id: str, r: dict[str, str]) -> MatchState | None:
        try:
            player1 = r.get("AF", "Unknown")
            player2 = r.get("AG", "Unknown")
            tournament = r.get("AH", r.get("CX", "Unknown"))

            # Surface from tournament name heuristics
            surface = _infer_surface(tournament)

            # Scores: Flashscore uses BA/BB for set1 home/away, BC/BD set2, etc.
            # Current set is inferred from status code (6=set1, 7=set2, ...)
            status = r.get("AE", "6")
            current_set = _status_to_set(status)

            set_keys = [("BA", "BB"), ("BC", "BD"), ("BE", "BF"), ("BG", "BH"), ("BI", "BJ")]
            sets_p1 = 0
            sets_p2 = 0
            games_p1, games_p2 = 0, 0

            for i, (hk, ak) in enumerate(set_keys[: current_set], start=1):
                hv = int(r.get(hk, "0") or "0")
                av = int(r.get(ak, "0") or "0")
                if i < current_set:
                    # Completed sets — determine winner
                    if hv > av:
                        sets_p1 += 1
                    elif av > hv:
                        sets_p2 += 1
                else:
                    games_p1, games_p2 = hv, av

            # Points in current game (DC = home points, DD = away points in some versions)
            # These fields may not always be present
            pts_p1 = r.get("DC", r.get("BQ", "0"))
            pts_p2 = r.get("DD", r.get("BR", "0"))

            # Serving: some feeds include this as "BF" or similar; skip if absent
            current_server = 0

            # Carry over game_log from existing state
            existing = None  # sync — we'll update async callers handle this
            game_log: list[int] = []

            is_tiebreak = (
                games_p1 >= 6 and games_p2 >= 6 and abs(games_p1 - games_p2) < 2
            )

            return MatchState(
                match_id=match_id,
                player1_name=player1,
                player2_name=player2,
                surface=surface,
                tournament=tournament,
                current_server=current_server,
                sets_p1=sets_p1,
                sets_p2=sets_p2,
                games_in_set_p1=games_p1,
                games_in_set_p2=games_p2,
                current_set=current_set,
                is_tiebreak=is_tiebreak,
                serve_stats_p1=ServeStats(),
                serve_stats_p2=ServeStats(),
                odds_p1=0.0,
                odds_p2=0.0,
                odds_history=[],
                game_log=game_log,
                match_duration_mins=0,
                timestamp=datetime.utcnow(),
            )
        except Exception as exc:
            log.debug("flashscore_parse_match_failed", match_id=match_id, error=str(exc))
            return None


def _status_to_set(status: str) -> int:
    """Map Flashscore status code to current set number."""
    mapping = {"6": 1, "7": 2, "8": 3, "9": 4, "10": 5, "31": 1}
    return mapping.get(status, 1)


def _infer_surface(tournament_name: str) -> str:
    """Infer surface from tournament name string."""
    name = tournament_name.lower()
    if "clay" in name:
        return "clay"
    if "grass" in name or "wimbledon" in name:
        return "grass"
    if "indoor" in name or "carpet" in name:
        return "indoor_hard"
    # Known clay tournaments by city
    if any(city in name for city in ("roland", "paris", "madrid", "rome", "barcelona",
                                      "monte", "hamburg", "lyon", "geneva", "estoril",
                                      "bordeaux", "cordoba", "buenos", "rio", "bogota")):
        return "clay"
    return "hard"
