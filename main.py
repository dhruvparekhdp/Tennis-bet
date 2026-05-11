"""
Entry point for the Tennis Bet prediction monitor.

Usage:
    python main.py

Environment:
    Copy .env.example to .env and fill in your credentials.
"""
import asyncio
import os
import signal

import structlog

from config.logging_config import configure_logging
from scheduler.health import start_health_server
from scheduler.runner import AppRunner
from storage.database import init_db

log = structlog.get_logger()


async def main() -> None:
    configure_logging()
    log.info("tennis_bet_starting")

    await init_db()
    log.info("database_initialised")

    # Render injects PORT; fall back to 8080 locally
    port = int(os.environ.get("PORT", 8080))

    runner = AppRunner()
    health_runner = await start_health_server(runner, port=port)

    stop_event = asyncio.Event()

    def _handle_signal() -> None:
        log.info("shutdown_signal_received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal)

    await runner.start()
    log.info("monitor_running", health_url=f"http://localhost:{port}/health")

    await stop_event.wait()

    log.info("shutting_down")
    await runner.stop()
    await health_runner.cleanup()
    log.info("shutdown_complete")


if __name__ == "__main__":
    asyncio.run(main())
