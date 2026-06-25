"""Standalone scheduler process (used by the `scheduler` docker service).

Runs the daily + live-refresh jobs in-process and blocks forever. The API service
runs with SCHEDULER_ENABLED=false so jobs execute in exactly one place.
"""
from __future__ import annotations

import signal
import time

from app.db.base import init_db
from app.logging_conf import configure_logging, get_logger
from app.scheduler.service import get_scheduler

logger = get_logger(__name__)


def main() -> None:
    configure_logging()
    init_db()
    scheduler = get_scheduler()
    scheduler.start()
    logger.info("Standalone scheduler running. Ctrl+C to stop.")

    stop = {"flag": False}

    def _handle(signum, frame):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    while not stop["flag"]:
        time.sleep(1)
    scheduler.shutdown()
    logger.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
