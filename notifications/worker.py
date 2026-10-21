"""Standalone worker: `python -m notifications.worker` (or the `notifications-worker` script).

Runs the same loop as the in-process worker; set WORKER_ENABLED=false on the API when you
run this separately so deliveries are not processed twice.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from notifications.config import get_settings
from notifications.database import init_db, make_engine, make_session_factory
from notifications.logging_config import configure_logging
from notifications.main import worker_loop
from notifications.services.context import AppContext

log = logging.getLogger(__name__)


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = make_engine(settings.database_url)
    init_db(engine)
    ctx = AppContext.from_settings(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    log.info(
        "worker started: poll=%ss batch=%d",
        settings.worker_poll_seconds,
        settings.worker_batch_size,
    )
    await worker_loop(make_session_factory(engine), ctx, stop)
    await ctx.http.aclose()
    log.info("worker stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover
    main()
