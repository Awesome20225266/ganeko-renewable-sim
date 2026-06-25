"""APScheduler-based automation.

Two jobs:
  * daily      — finalize yesterday (HISTORICAL) + refresh today + build the
                 forecast horizon (+1..+7 days) for every active plant.
  * live-refresh — re-run today's LIVE simulation every LIVE_REFRESH_MINUTES.

A cron / Celery-beat alternative is documented in the README. All jobs are
idempotent and can be triggered manually for any date.
"""
from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo
from datetime import datetime

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
        self.scheduler.start()
        self._started = True
        logger.info(
            "Scheduler started (daily=%s %s, live every %dmin)",
            settings.SCHEDULER_DAILY_TIME, tz, settings.LIVE_REFRESH_MINUTES,
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
