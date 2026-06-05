#!/usr/bin/env python3
"""
Quick test of Sportradar Tennis API v3 from within Render.
Run this on your Render server to diagnose the 403 issue.

Usage:
  python test_sportradar.py YOUR_SPORTRADAR_KEY
"""
import asyncio
import json
import sys
import httpx


async def test():
    if len(sys.argv) < 2:
        print("Usage: python test_sportradar.py YOUR_KEY")
        sys.exit(1)

    key = sys.argv[1]
    headers = {"x-api-key": key}

    endpoints = [
        ("Live summaries (v3)", "https://api.sportradar.com/tennis/trial/v3/en/schedules/live/summaries.json"),
        ("Schedule (v3)", "https://api.sportradar.com/tennis/trial/v3/en/schedules/2026-06-05/schedule.json"),
    ]

    async with httpx.AsyncClient(timeout=15) as client:
        for name, url in endpoints:
            print(f"\n{'='*60}")
            print(f"Testing: {name}")
            print(f"URL: {url}")
            print(f"Header: x-api-key: {key[:10]}...")
            print('='*60)

            try:
                resp = await client.get(url, headers=headers)
                print(f"Status: {resp.status_code}")
                print(f"Headers: {dict(resp.headers)}")

                if resp.status_code == 200:
                    data = resp.json()
                    print(f"\n✅ SUCCESS!")
                    print(f"Response keys: {list(data.keys())}")
                    if "schedule_live_summaries" in data:
                        matches = data["schedule_live_summaries"].get("sport_events", [])
                        print(f"Live matches found: {len(matches)}")
                        for m in matches[:3]:
                            h = m.get("competitors", [{}])[0].get("name", "?")
                            a = m.get("competitors", [{}])[1].get("name", "?") if len(m.get("competitors", [])) > 1 else "?"
                            print(f"  - {h} vs {a}")
                    elif "schedule" in data:
                        matches = data["schedule"].get("sport_events", [])
                        print(f"Scheduled matches found: {len(matches)}")
                    print(f"\nFull response (first 1000 chars):")
                    print(json.dumps(data, indent=2)[:1000])
                elif resp.status_code == 401:
                    print(f"\n❌ 401 Unauthorized — API key is invalid or revoked")
                    print(f"Response: {resp.text[:300]}")
                elif resp.status_code == 403:
                    print(f"\n❌ 403 Forbidden — either:")
                    print(f"  1. Trial has expired (30 days from signup)")
                    print(f"  2. IP is blocked (shouldn't happen from Render)")
                    print(f"Response: {resp.text[:300]}")
                else:
                    print(f"\n❌ {resp.status_code} Error")
                    print(f"Response: {resp.text[:300]}")
            except Exception as e:
                print(f"\n❌ Exception: {e}")


if __name__ == "__main__":
    asyncio.run(test())
