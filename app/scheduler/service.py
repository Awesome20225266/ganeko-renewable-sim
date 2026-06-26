"""APScheduler-based automation.

Two jobs:
  * daily      — finalize yesterday (HISTORICAL) + refresh today + build the
                 forecast horizon (+1..+7 days) for every active plant.
  * live-refresh — re-run today's LIVE simulation every LIVE_REFRESH_MINUTES.

A cron / Celery-beat alternative is documented in the README. All jobs are
idempotent and can be triggered manually for any date.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.config.settings import get_settings
from app.db.base import session_scope
from app.db.models import Plant
from app.logging_conf import get_logger
from app.simulate import run_simulation_sync
from app.weather.client import DataMode

logger = get_logger(__name__)

FORECAST_HORIZON_DAYS = 7


def _active_plants() -> list[tuple[str, str]]:
    with session_scope() as db:
        rows = list(db.scalars(select(Plant)))
        return [(p.plant_code, _plant_tz(p.plant_code)) for p in rows]


def _plant_tz(plant_code: str) -> str:
    from app.simulate import load_active_config

    with session_scope() as db:
        return load_active_config(db, plant_code).timezone


def run_daily_job() -> None:
    """Finalize yesterday, refresh today, and build the forecast horizon."""
    logger.info("Daily job starting")
    for plant_code, tz in _active_plants():
        today = datetime.now(ZoneInfo(tz)).date()
        yesterday = today - timedelta(days=1)
        try:
            run_simulation_sync(
                plant_code, yesterday, DataMode.HISTORICAL,
                triggered_by="scheduler", force_refetch=True,
            )
            run_simulation_sync(
                plant_code, today, DataMode.LIVE,
                triggered_by="scheduler", force_refetch=True,
            )
            for h in range(1, FORECAST_HORIZON_DAYS + 1):
                run_simulation_sync(
                    plant_code, today + timedelta(days=h), DataMode.FORECAST,
                    triggered_by="scheduler", force_refetch=True,
                )
            logger.info("Daily job done for plant=%s", plant_code)
        except Exception as exc:  # noqa: BLE001
            logger.error("Daily job failed for plant=%s: %s", plant_code, exc)


def run_live_refresh() -> None:
    """Re-run today's LIVE simulation for every active plant."""
    for plant_code, tz in _active_plants():
        today = datetime.now(ZoneInfo(tz)).date()
        try:
            run_simulation_sync(
                plant_code, today, DataMode.LIVE,
                triggered_by="scheduler", force_refetch=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Live refresh failed for plant=%s: %s", plant_code, exc)


def run_for_date(plant_code: str, sim_date: date, mode: DataMode | None = None) -> None:
    """Manual trigger for any date (used by the CLI / admin)."""
    run_simulation_sync(plant_code, sim_date, mode, triggered_by="manual", force_refetch=True)


def _keepalive_url() -> str:
    """Public base URL to self-ping (explicit setting wins, else the host's env var)."""
    settings = get_settings()
    base = settings.KEEPALIVE_URL or os.environ.get("RENDER_EXTERNAL_URL", "")
    return base.rstrip("/")


def run_keepalive() -> None:
    """Self-ping /health so a free-tier host never spins down on idle.

    A no-op if no public URL is known. Failures are swallowed — a missed ping just
    means the host may sleep until the next inbound request wakes it.
    """
    base = _keepalive_url()
    if not base:
        logger.warning("keepalive enabled but no URL (set KEEPALIVE_URL or RENDER_EXTERNAL_URL)")
        return
    try:
        r = httpx.get(f"{base}/health", timeout=10.0)
        logger.debug("keepalive ping %s -> %s", base, r.status_code)
    except Exception as exc:  # noqa: BLE001 — never let a ping failure surface
        logger.warning("keepalive ping failed: %s", exc)


class SchedulerService:
    def __init__(self):
        self.scheduler = BackgroundScheduler(timezone="UTC")
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        settings = get_settings()
        hh, mm = settings.SCHEDULER_DAILY_TIME.split(":")
        plants = _active_plants()
        tz = plants[0][1] if plants else "UTC"
        self.scheduler.add_job(
            run_daily_job,
            CronTrigger(hour=int(hh), minute=int(mm), timezone=ZoneInfo(tz)),
            id="daily_job",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        self.scheduler.add_job(
            run_live_refresh,
            IntervalTrigger(minutes=max(1, settings.LIVE_REFRESH_MINUTES)),
            id="live_refresh",
            replace_existing=True,
            misfire_grace_time=300,
        )
        if settings.KEEPALIVE_ENABLED:
            self.scheduler.add_job(
                run_keepalive,
                IntervalTrigger(minutes=max(1, settings.KEEPALIVE_MINUTES)),
                id="keepalive",
                replace_existing=True,
                misfire_grace_time=120,
                next_run_time=datetime.now(ZoneInfo("UTC")),  # ping immediately on boot
            )
        self.scheduler.start()
        self._started = True
        logger.info(
            "Scheduler started (daily=%s %s, live every %dmin, keepalive=%s)",
            settings.SCHEDULER_DAILY_TIME, tz, settings.LIVE_REFRESH_MINUTES,
            settings.KEEPALIVE_ENABLED,
        )

    def shutdown(self) -> None:
        if self._started:
            self.scheduler.shutdown(wait=False)
            self._started = False


_service: SchedulerService | None = None


def get_scheduler() -> SchedulerService:
    global _service
    if _service is None:
        _service = SchedulerService()
    return _service
