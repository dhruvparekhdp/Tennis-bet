"""
Flashscore internal feed collector.

Flashscore exposes a proprietary live feed at d.flashscore.in/x/feed/
used by their own website. Covers ATP, WTA, Challengers, ITF — far broader
than ESPN. Response uses a custom ¬/÷ text format.

Session strategy:
  1. GET the main tennis page first to obtain Cloudflare cookies (cf_clearance,
     __cf_bm, etc.) and any consent cookies.
  2. Reuse that cookie jar for the data feed request.

This mirrors what a real browser does and avoids the "0" empty response.
"""
import asyncio
import re
from datetime import datetime

import httpx
import structlog

from analysis.match_state import MatchState, OddsPoint, ServeStats
from analysis.state_store import MatchStateStore
from collectors.base import BaseCollector

log = structlog.get_logger()

_HOME_URL = "https://www.flashscore.in/tennis/"
_FEED_URL = "https://d.flashscore.in/x/feed/f_1_5_0_en_1"

# Also try the .com global domain as fallback
_FEED_URL_COM = "https://d.flashscore.com/x/feed/f_1_5_0_en_1"

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

_FEED_HEADERS = {
    **_BASE_HEADERS,
    "X-GeoIP": "1",
    "X-Fsign": "SW9D1eZo",
    "Accept": "*/*",
    "Referer": "https://www.flashscore.in/tennis/",
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


def _extract_fsign(html: str) -> str | None:
    """Try to extract a fresh X-Fsign token from the Flashscore JS bundle reference."""
    m = re.search(r'["\']([A-Za-z0-9]{8,16})["\']', html)
    return m.group(1) if m else None


class FlashscoreCollector(BaseCollector):
    def __init__(self, store: MatchStateStore) -> None:
        self.store = store
        self._consecutive_failures = 0
        self._consecutive_zero_matches = 0
        self._cookies: dict[str, str] = {}

    async def _init_session(self, client: httpx.AsyncClient) -> None:
        """
        Fetch the Flashscore tennis homepage to get valid Cloudflare cookies.
        Sets self._cookies for subsequent feed requests.
        """
        try:
            resp = await client.get(
                _HOME_URL,
                headers={**_BASE_HEADERS, "Accept": "text/html,application/xhtml+xml,*/*"},
                follow_redirects=True,
            )
            # httpx stores cookies in the client's cookie jar automatically
            if resp.status_code == 200:
                log.debug("flashscore_session_ok", cookies=list(client.cookies.keys()))
            else:
                log.debug("flashscore_session_status", status=resp.status_code)
        except Exception as exc:
            log.debug("flashscore_session_failed", error=str(exc))

    async def fetch(self) -> None:
        if self._consecutive_failures >= 5:
            return  # silently stopped — blocked from this cloud IP

        try:
            async with httpx.AsyncClient(
                headers=_BASE_HEADERS,
                timeout=20.0,
                follow_redirects=True,
            ) as client:
                # Step 1: warm up session / get Cloudflare cookies
                await self._init_session(client)
                await asyncio.sleep(0.5)  # small jitter

                # Step 2: try .in feed first, fall back to .com
                raw = await self._fetch_feed(client, _FEED_URL)
                if raw == "0" or not raw.strip():
                    log.debug("flashscore_in_empty_trying_com")
                    raw = await self._fetch_feed(client, _FEED_URL_COM)

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

        live_ids: set[str] = set()
        matches_found = 0

        all_blocks = [b for b in raw.split("~") if b.strip()]
        all_records = [_parse_record(b) for b in all_blocks]
        non_empty_records = [r for r in all_records if r]

        all_statuses: set[str] = set()
        has_aa: int = 0
        for r in non_empty_records:
            if "AE" in r:
                all_statuses.add(r["AE"])
            if "AA" in r:
                has_aa += 1

        for record in non_empty_records:
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

        if matches_found == 0:
            self._consecutive_zero_matches += 1
            log.warning(
                "flashscore_zero_matches",
                raw_len=len(raw),
                total_blocks=len(all_blocks),
                non_empty_records=len(non_empty_records),
                records_with_AA=has_aa,
                all_AE_values_seen=sorted(all_statuses),
                in_progress_statuses_expected=sorted(_IN_PROGRESS_STATUSES),
                sample_raw=raw[:200],
                consecutive_zeros=self._consecutive_zero_matches,
            )
        else:
            self._consecutive_zero_matches = 0

        log.info("flashscore_collector_done", live_matches=len(live_ids),
                 total_records=matches_found)

    async def _fetch_feed(self, client: httpx.AsyncClient, url: str) -> str:
        resp = await client.get(url, headers=_FEED_HEADERS)
        resp.raise_for_status()
        log.debug("flashscore_feed_response", url=url, status=resp.status_code,
                  raw_len=len(resp.text), sample=resp.text[:100])
        return resp.text

    def _build_state(self, match_id: str, r: dict[str, str]) -> MatchState | None:
        try:
            player1 = r.get("AF", "Unknown")
            player2 = r.get("AG", "Unknown")
            tournament = r.get("AH", r.get("CX", "Unknown"))

            surface = _infer_surface(tournament)

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
                    if hv > av:
                        sets_p1 += 1
                    elif av > hv:
                        sets_p2 += 1
                else:
                    games_p1, games_p2 = hv, av

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
                current_server=0,
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
    mapping = {"6": 1, "7": 2, "8": 3, "9": 4, "10": 5, "31": 1}
    return mapping.get(status, 1)


def _infer_surface(tournament_name: str) -> str:
    name = tournament_name.lower()
    if "clay" in name:
        return "clay"
    if "grass" in name or "wimbledon" in name:
        return "grass"
    if "indoor" in name or "carpet" in name:
        return "indoor_hard"
    if any(city in name for city in ("roland", "paris", "madrid", "rome", "barcelona",
                                      "monte", "hamburg", "lyon", "geneva", "estoril",
                                      "bordeaux", "cordoba", "buenos", "rio", "bogota")):
        return "clay"
    return "hard"
