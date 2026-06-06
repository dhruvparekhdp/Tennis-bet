"""
Standalone Odds API debugger — run directly to diagnose what the API is returning.

Usage:
    python debug_odds_api.py                   # uses ODDS_API_KEY from env/.env
    python debug_odds_api.py --key YOUR_KEY    # explicit key
    python debug_odds_api.py --key YOUR_KEY --sport tennis_atp_french_open
    python debug_odds_api.py --key YOUR_KEY --all-sports  # list every tennis sport key

This does NOT start the web server — just prints a full diagnostic report.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone


async def run(api_key: str, target_sport: str | None = None, list_sports: bool = False) -> None:
    try:
        import httpx
    except ImportError:
        print("ERROR: pip install httpx")
        sys.exit(1)

    BASE = "https://api.the-odds-api.com/v4"
    now = datetime.now(timezone.utc)

    print(f"\n{'='*60}")
    print(f"  Odds API Debugger  —  {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"{'='*60}\n")

    async with httpx.AsyncClient(timeout=20) as c:

        # ── 1. Sports list ────────────────────────────────────────────────────
        print("STEP 1: GET /v4/sports/?all=true  (free, no quota cost)")
        r = await c.get(f"{BASE}/sports/", params={"apiKey": api_key, "all": "true"})
        print(f"  HTTP {r.status_code}")
        if r.status_code != 200:
            print(f"  ERROR body: {r.text[:300]}")
            return

        all_sports: list[dict] = r.json()
        tennis_sports = [s for s in all_sports if "tennis" in s.get("key", "").lower()]
        active_tennis = [s for s in tennis_sports if s.get("active")]
        inactive_tennis = [s for s in tennis_sports if not s.get("active")]

        print(f"\n  Total sports in catalogue: {len(all_sports)}")
        print(f"  Tennis sports total:  {len(tennis_sports)}")
        print(f"  Tennis ACTIVE:        {len(active_tennis)}")
        for s in active_tennis:
            print(f"    ✅ {s['key']:45s}  {s.get('title','')}")
        if inactive_tennis and list_sports:
            print(f"\n  Tennis INACTIVE ({len(inactive_tennis)}):")
            for s in inactive_tennis:
                print(f"    ⬜ {s['key']:45s}  {s.get('title','')}")
        elif inactive_tennis:
            print(f"  (+ {len(inactive_tennis)} inactive tennis keys — use --all-sports to show)")

        if not active_tennis:
            print("\n  ⚠️  NO ACTIVE TENNIS SPORTS right now.")
            print("  This means no bookmaker currently has open tennis odds.")
            print("  This is normal between tournaments. Try again during:")
            print("    • Roland Garros (May–Jun)  • Wimbledon (Jul)")
            print("    • US Open (Aug–Sep)        • Australian Open (Jan)")
            print("    • ATP/WTA 1000 Masters events throughout the year\n")
            if not target_sport:
                return

        sports_to_query = ([target_sport] if target_sport
                           else [s["key"] for s in active_tennis])

        # ── 2. Odds per sport ─────────────────────────────────────────────────
        print(f"\nSTEP 2: GET /v4/sports/{{sport}}/odds/  ({len(sports_to_query)} sport(s))")
        print(f"  regions={os.environ.get('ODDS_REGIONS','eu')}  markets=h2h  "
              f"window: -{12}h to +{24}h\n")

        total_events = 0
        commence_from = (now - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
        commence_to = (now + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")

        for sport_key in sports_to_query:
            region = os.environ.get("ODDS_REGIONS", "eu")
            r2 = await c.get(
                f"{BASE}/sports/{sport_key}/odds/",
                params={
                    "apiKey": api_key,
                    "regions": region,
                    "markets": "h2h",
                    "oddsFormat": "decimal",
                    "commenceTimeFrom": commence_from,
                    "commenceTimeTo": commence_to,
                },
            )
            quota_rem = r2.headers.get("x-requests-remaining", "?")
            quota_used = r2.headers.get("x-requests-used", "?")
            print(f"  [{sport_key}]  HTTP {r2.status_code}  "
                  f"quota used={quota_used}  remaining={quota_rem}")

            if r2.status_code == 404:
                print(f"    → 404: sport not found / not in season")
                continue
            if r2.status_code == 401:
                print(f"    → 401: invalid API key")
                continue
            if r2.status_code == 422:
                print(f"    → 422 Unprocessable: {r2.text[:200]}")
                continue
            if r2.status_code != 200:
                print(f"    → ERROR: {r2.text[:200]}")
                continue

            events: list[dict] = r2.json()
            total_events += len(events)

            if not events:
                print(f"    → 0 events (sport active but no odds in this time window)")
                continue

            for ev in events:
                home = ev.get("home_team", "?")
                away = ev.get("away_team", "?")
                ct = ev.get("commence_time", "")
                bk = len(ev.get("bookmakers", []))
                try:
                    ct_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                    mins = int((ct_dt - now).total_seconds() / 60)
                    timing = f"LIVE {-mins}m ago" if mins <= 0 else f"starts in {mins}m"
                except Exception:
                    timing = ct

                # Extract odds
                h_price = a_price = 0.0
                for bm in ev.get("bookmakers", []):
                    for mkt in bm.get("markets", []):
                        if mkt.get("key") != "h2h":
                            continue
                        for oc in mkt.get("outcomes", []):
                            nm = oc.get("name", "").lower()
                            pr = float(oc.get("price", 0))
                            if nm == home.lower() and pr > h_price:
                                h_price = pr
                            elif nm == away.lower() and pr > a_price:
                                a_price = pr

                odds_str = (f"  odds: {home}={h_price:.2f} / {away}={a_price:.2f}"
                            if h_price else "  odds: no bookmaker data")
                print(f"    ✅ {home:25s} vs {away:25s}  [{timing}]  bookmakers={bk}")
                print(f"       {odds_str}")

        print(f"\n  TOTAL events found: {total_events}")

        # ── 3. Summary ────────────────────────────────────────────────────────
        print(f"\n{'='*60}")
        print("  SUMMARY")
        print(f"{'='*60}")
        if total_events == 0 and not active_tennis:
            print("  ❌ No data: no active tennis sports right now.")
            print("     Check back during a tournament (Roland Garros ends ~8 Jun 2026).")
        elif total_events == 0 and active_tennis:
            print("  ⚠️  Active tennis sports exist but no events in the 12h–+24h window.")
            print("     This usually means your free-tier plan doesn't cover these markets,")
            print("     OR no matches are scheduled in this specific time range.")
            print(f"     Try widening: commenceTimeFrom=-48h commenceTimeTo=+48h")
        else:
            print(f"  ✅ {total_events} events found across {len(sports_to_query)} sport(s).")
            print("     Your API key + plan has tennis coverage.")
        print()


def main() -> None:
    p = argparse.ArgumentParser(description="Debug The Odds API tennis data")
    p.add_argument("--key", default=os.environ.get("ODDS_API_KEY"),
                   help="API key (or set ODDS_API_KEY env var / .env file)")
    p.add_argument("--sport", dest="sport",
                   help="Specific sport key to query (e.g. tennis_atp_french_open)")
    p.add_argument("--all-sports", action="store_true",
                   help="Show all tennis keys including inactive ones")
    args = p.parse_args()

    # Try loading from .env if no key given
    if not args.key:
        try:
            from dotenv import load_dotenv
            load_dotenv()
            args.key = os.environ.get("ODDS_API_KEY")
        except ImportError:
            pass

    if not args.key:
        print("ERROR: Provide API key via --key or ODDS_API_KEY env var")
        sys.exit(1)

    asyncio.run(run(args.key, target_sport=args.sport, list_sports=args.all_sports))


if __name__ == "__main__":
    main()
