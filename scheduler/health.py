"""Lightweight HTTP health endpoint on port 8080."""
import asyncio
import json
from datetime import datetime

from aiohttp import web

from analysis.state_store import MatchStateStore

_start_time = datetime.utcnow()


async def make_health_app(store: MatchStateStore) -> web.Application:
    app = web.Application()

    async def health(request: web.Request) -> web.Response:
        matches_tracked = await store.count()
        uptime_secs = int((datetime.utcnow() - _start_time).total_seconds())
        body = json.dumps({
            "status": "ok",
            "matches_tracked": matches_tracked,
            "uptime_seconds": uptime_secs,
        })
        return web.Response(text=body, content_type="application/json")

    app.router.add_get("/health", health)
    return app


async def start_health_server(store: MatchStateStore, port: int = 8080) -> asyncio.Task:
    app = await make_health_app(store)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner
