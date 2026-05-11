"""
TheSportsDB collector — fetches today's tennis schedule for pre-match context.
Free API key "3" works for basic lookups.
"""
from datetime import date

import httpx
import structlog

from collectors.base import BaseCollector

log = structlog.get_logger()

_BASE = "https://www.thesportsdb.com/api/v1/json"


class TheSportsDBCollector(BaseCollector):
    def __init__(self, api_key: str = "3") -> None:
        self.api_key = api_key
        self._today_events: list[dict] = []

    async def fetch(self) -> None:
        today = date.today().strftime("%Y-%m-%d")
        url = f"{_BASE}/{self.api_key}/eventsday.php?d={today}&s=Tennis"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
                self._today_events = data.get("events") or []
                log.info("thesportsdb_fetched", count=len(self._today_events), date=today)
        except Exception:
            log.exception("thesportsdb_fetch_failed")

    def get_today_events(self) -> list[dict]:
        return self._today_events
