#!/usr/bin/env python3
"""
Flashscore push client — runs on your laptop (residential IP bypasses cloud blocks)
and forwards live match data to the Render server every 30 seconds.

Setup:
    cd Tennis-bet
    pip install httpx structlog aiohttp   # or: pip install -r requirements.txt

Usage:
    python push_client.py --server https://tennis-bet-izye.onrender.com --key YOUR_KEY

    # Or via env vars:
    PUSH_SERVER_URL=https://tennis-bet-izye.onrender.com INGEST_API_KEY=your-key python push_client.py

The --key / INGEST_API_KEY must match the INGEST_API_KEY set in Render's environment variables.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime

import httpx
import structlog

# Allow running from the project root without installing the package
sys.path.insert(0, os.path.dirname(__file__))

from analysis.match_state import MatchState
from analysis.state_store import MatchStateStore
from collectors.flashscore import FlashscoreCollector

log = structlog.get_logger()

POLL_INTERVAL_SECS = 30


def _state_to_dict(s: MatchState) -> dict:
    return {
        "match_id": s.match_id,
        "player1_name": s.player1_name,
        "player2_name": s.player2_name,
        "surface": s.surface,
        "tournament": s.tournament,
        "current_server": s.current_server,
        "sets_p1": s.sets_p1,
        "sets_p2": s.sets_p2,
        "games_in_set_p1": s.games_in_set_p1,
        "games_in_set_p2": s.games_in_set_p2,
        "current_set": s.current_set,
        "is_tiebreak": s.is_tiebreak,
        "serve_stats_p1": {
            "first_serve_pct": s.serve_stats_p1.first_serve_pct,
            "aces": s.serve_stats_p1.aces,
            "double_faults": s.serve_stats_p1.double_faults,
        },
        "serve_stats_p2": {
            "first_serve_pct": s.serve_stats_p2.first_serve_pct,
            "aces": s.serve_stats_p2.aces,
            "double_faults": s.serve_stats_p2.double_faults,
        },
        "odds_p1": s.odds_p1,
        "odds_p2": s.odds_p2,
        "game_log": s.game_log,
        "match_duration_mins": s.match_duration_mins,
        "timestamp": s.timestamp.isoformat(),
        "is_scheduled": s.is_scheduled,
        "start_time": s.start_time.isoformat() if s.start_time else None,
    }


async def push_loop(server_url: str, api_key: str, use_parimatch: bool = False) -> None:
    store = MatchStateStore()
    flashscore = FlashscoreCollector(store)
    parimatch = None
    if use_parimatch:
        from collectors.parimatch import ParimatchCollector
        parimatch = ParimatchCollector(store=store, headless=True)
        log.info("parimatch_enabled")
    consecutive_push_failures = 0

    log.info("push_client_started", server=server_url, poll_interval=POLL_INTERVAL_SECS)

    while True:
        try:
            # Scrape Flashscore locally (residential IP — not blocked)
            await flashscore.fetch()
            # Optionally scrape Parimatch (Playwright) into the same store
            if parimatch is not None:
                try:
                    await parimatch.fetch_states()
                except Exception:
                    log.exception("parimatch_scrape_failed")
            states = await store.get_all()

            if not states:
                log.info("no_live_matches_found")
            else:
                log.info("matches_scraped", count=len(states),
                         matches=[f"{s.player1_name} vs {s.player2_name}" for s in states])

            # Push to Render
            payload = {"matches": [_state_to_dict(s) for s in states]}
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{server_url.rstrip('/')}/api/ingest",
                    json=payload,
                    headers={"X-Ingest-Key": api_key},
                )

            if resp.status_code == 200:
                result = resp.json()
                consecutive_push_failures = 0
                log.info("push_ok", sent=len(states), server_accepted=result.get("count"))
            elif resp.status_code == 401:
                log.error("push_unauthorized",
                          hint="Check --key matches INGEST_API_KEY on Render")
                break  # No point retrying — fix the key first
            else:
                consecutive_push_failures += 1
                log.warning("push_failed", status=resp.status_code,
                            body=resp.text[:200],
                            consecutive_failures=consecutive_push_failures)

        except httpx.ConnectError:
            consecutive_push_failures += 1
            log.warning("server_unreachable", server=server_url,
                        hint="Is the Render server running?",
                        consecutive_failures=consecutive_push_failures)
        except Exception:
            log.exception("push_cycle_error")

        await asyncio.sleep(POLL_INTERVAL_SECS)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Forward Flashscore live scores from your laptop to the Render server."
    )
    parser.add_argument(
        "--server",
        default=os.getenv("PUSH_SERVER_URL", "https://tennis-bet-izye.onrender.com"),
        help="Render server base URL (default: tennis-bet-izye.onrender.com)",
    )
    parser.add_argument(
        "--key",
        default=os.getenv("INGEST_API_KEY", ""),
        help="Ingest API key — must match INGEST_API_KEY in Render env vars",
    )
    parser.add_argument(
        "--parimatch",
        action="store_true",
        help="Also scrape Parimatch via Playwright (needs: playwright install chromium)",
    )
    args = parser.parse_args()

    if not args.key:
        print("No API key provided — connecting without auth (server must have INGEST_API_KEY unset).")

    try:
        asyncio.run(push_loop(args.server, args.key, use_parimatch=args.parimatch))
    except KeyboardInterrupt:
        print("\nPush client stopped.")


if __name__ == "__main__":
    main()
