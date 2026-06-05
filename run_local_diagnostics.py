#!/usr/bin/env python3
"""
Local diagnostics — run this in VSCode terminal to test all APIs.
No cloud IP blocking. Tests with your actual keys.

Usage:
  python run_local_diagnostics.py

This will:
1. Test Sportradar live + schedule endpoints
2. Test Odds API quota
3. Test ESPN endpoints
4. Test API-Sports (if key is set)
5. Show what env vars to set in Render
"""
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

# Load .env if exists
env_file = Path(".env")
if env_file.exists():
    from dotenv import load_dotenv
    load_dotenv()

import httpx


class Colors:
    OK = "\033[92m"
    FAIL = "\033[91m"
    WARN = "\033[93m"
    INFO = "\033[94m"
    RESET = "\033[0m"


def print_header(title: str) -> None:
    print(f"\n{Colors.INFO}{'='*70}{Colors.RESET}")
    print(f"{Colors.INFO}{title:^70}{Colors.RESET}")
    print(f"{Colors.INFO}{'='*70}{Colors.RESET}\n")


def print_ok(msg: str) -> None:
    print(f"{Colors.OK}✅ {msg}{Colors.RESET}")


def print_fail(msg: str) -> None:
    print(f"{Colors.FAIL}❌ {msg}{Colors.RESET}")


def print_warn(msg: str) -> None:
    print(f"{Colors.WARN}⚠️  {msg}{Colors.RESET}")


def print_info(msg: str) -> None:
    print(f"{Colors.INFO}ℹ️  {msg}{Colors.RESET}")


async def test_sportradar():
    """Test Sportradar Tennis API v3."""
    print_header("SPORTRADAR TENNIS API")

    key = os.environ.get("SPORTRADAR_API_KEY")
    if not key:
        print_warn("SPORTRADAR_API_KEY not set in .env")
        print_info("Skipping Sportradar test")
        return False

    print_info(f"Key: {key[:15]}...")

    headers = {"x-api-key": key}
    tests = [
        ("Live matches", "https://api.sportradar.com/tennis/trial/v3/en/schedules/live/summaries.json", None),
        ("Today's schedule", "https://api.sportradar.com/tennis/trial/v3/en/schedules/2026-06-05/schedule.json", None),
    ]

    async with httpx.AsyncClient(timeout=15) as client:
        for name, url, params in tests:
            try:
                resp = await client.get(url, headers=headers, params=params)

                if resp.status_code == 200:
                    data = resp.json()
                    if "schedule_live_summaries" in data:
                        events = data["schedule_live_summaries"].get("sport_events", [])
                    elif "schedule" in data:
                        events = data["schedule"].get("sport_events", [])
                    else:
                        events = []

                    print_ok(f"{name}: {len(events)} matches")
                    for e in events[:2]:
                        comps = e.get("competitors", [])
                        h = comps[0].get("name", "?") if comps else "?"
                        a = comps[1].get("name", "?") if len(comps) > 1 else "?"
                        print(f"   • {h} vs {a}")
                elif resp.status_code == 401:
                    print_fail(f"{name}: 401 Unauthorized — Key is INVALID/REVOKED")
                    return False
                elif resp.status_code == 403:
                    print_fail(f"{name}: 403 Forbidden — 30-day trial EXPIRED")
                    print_warn("Sign up for new trial: https://developer.sportradar.com/")
                    return False
                else:
                    print_fail(f"{name}: {resp.status_code}")
                    print(f"   Response: {resp.text[:200]}")
                    return False
            except Exception as e:
                print_fail(f"{name}: {e}")
                return False

    return True


async def test_odds_api():
    """Test Odds API quota."""
    print_header("ODDS API")

    key = os.environ.get("ODDS_API_KEY")
    if not key:
        print_warn("ODDS_API_KEY not set in .env")
        print_info("Skipping Odds API test")
        return None

    print_info(f"Key: {key[:15]}...")

    async with httpx.AsyncClient(timeout=15) as client:
        # Test a simple sports list (free, no quota cost)
        try:
            resp = await client.get(
                "https://api.the-odds-api.com/v4/sports/",
                params={"apiKey": key, "all": "true"}
            )

            if resp.status_code == 200:
                quota_used = resp.headers.get("x-requests-used", "?")
                quota_remaining = resp.headers.get("x-requests-remaining", "?")
                data = resp.json()
                tennis = [s for s in data if "tennis" in s.get("key", "").lower()]
                active = [s for s in tennis if s.get("active")]

                print_ok(f"API key valid")
                print(f"   Quota used this month: {quota_used}")
                print(f"   Quota remaining: {quota_remaining}")
                print(f"   Active tennis sports: {len(active)}")

                if quota_remaining == "0":
                    print_warn("⚠️  Quota EXHAUSTED for this month")
                    print_warn("   Free tier: 500 req/month")
                    print_warn("   Resets: 1st of next month at 12 AM UTC")
                    return False

                for s in active[:3]:
                    print(f"   • {s['key']}")

                return True
            elif resp.status_code == 401:
                print_fail("401 Unauthorized — Key is INVALID")
                return False
            else:
                print_fail(f"{resp.status_code}: {resp.text[:200]}")
                return False
        except Exception as e:
            print_fail(f"Error: {e}")
            return False


async def test_espn():
    """Test ESPN endpoints."""
    print_header("ESPN TENNIS API")

    urls = [
        ("ATP live", "https://site.api.espn.com/apis/site/v2/sports/tennis/atp/scoreboard"),
        ("WTA live", "https://site.api.espn.com/apis/site/v2/sports/tennis/wta/scoreboard"),
        ("French Open", "https://site.api.espn.com/apis/site/v2/sports/tennis/french-open/scoreboard"),
    ]

    async with httpx.AsyncClient(timeout=15) as client:
        for name, url in urls:
            try:
                resp = await client.get(url, params={"limit": "5"})

                if resp.status_code == 200:
                    data = resp.json()
                    events = data.get("events", [])
                    print_ok(f"{name}: {len(events)} matches")
                    for e in events[:1]:
                        print(f"   • {e.get('name', '?')}")
                elif resp.status_code == 403:
                    print_fail(f"{name}: 403 — Blocked (expected from cloud IP)")
                else:
                    print_warn(f"{name}: {resp.status_code}")
            except Exception as e:
                print_fail(f"{name}: {e}")

        print_info("ESPN is cloud-safe but may block from datacenter IPs.")
        print_info("Should work fine from Render.")


async def test_api_sports():
    """Test API-Sports Tennis."""
    print_header("API-SPORTS TENNIS")

    key = os.environ.get("API_SPORTS_KEY")
    if not key:
        print_warn("API_SPORTS_KEY not set in .env")
        print_info("Get free key at: https://dashboard.api-sports.io")
        return None

    print_info(f"Key: {key[:15]}...")

    headers = {
        "x-apisports-key": key,
        "x-apisports-host": "v1.tennis.api-sports.io",
    }

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(
                "https://v1.tennis.api-sports.io/games",
                params={"live": "all"},
                headers=headers
            )

            if resp.status_code == 200:
                data = resp.json()
                games = data.get("response", [])
                quota = resp.headers.get("x-ratelimit-requests-remaining", "?")

                print_ok(f"API key valid")
                print(f"   Live games: {len(games)}")
                print(f"   Quota remaining today: {quota}")

                for g in games[:2]:
                    h = g.get("teams", {}).get("home", {}).get("name", "?")
                    a = g.get("teams", {}).get("away", {}).get("name", "?")
                    print(f"   • {h} vs {a}")

                return True
            elif resp.status_code == 401:
                print_fail("401 Unauthorized — Key is INVALID")
                return False
            else:
                print_fail(f"{resp.status_code}: {resp.text[:200]}")
                return False
        except Exception as e:
            print_fail(f"Error: {e}")
            return False


async def main():
    print(f"\n{Colors.INFO}Tennis-Bet Local Diagnostics{Colors.RESET}")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}\n")

    results = {}

    results["sportradar"] = await test_sportradar()
    results["odds_api"] = await test_odds_api()
    await test_espn()
    results["api_sports"] = await test_api_sports()

    print_header("SUMMARY & NEXT STEPS")

    if results["sportradar"]:
        print_ok("Sportradar is working! Update Render env var:")
        print(f"   SPORTRADAR_API_KEY = (your key)")
    elif results["sportradar"] is False:
        print_warn("Sportradar needs attention (key invalid or trial expired)")

    if results["odds_api"] is True:
        print_ok("Odds API is working!")
    elif results["odds_api"] is False:
        print_warn("Odds API quota exhausted or key invalid")

    if results["api_sports"] is True:
        print_ok("API-Sports is working! Update Render env var:")
        print(f"   API_SPORTS_KEY = (your key)")
    elif results["api_sports"] is False:
        print_warn("API-Sports key invalid")

    print()
    print_info("To update Render env vars:")
    print("   1. Go to https://dashboard.render.com")
    print("   2. Select Tennis-bet service → Environment")
    print("   3. Set keys, then 'Save, rebuild, and deploy'")
    print()


if __name__ == "__main__":
    asyncio.run(main())
