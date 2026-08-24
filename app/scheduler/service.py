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

from app.config.settings import Settings, get_settings
from app.db.base import session_scope
from app.db.models import ErrorLog, Plant
from app.logging_conf import get_logger
from app.simulate import ensure_fresh_live, run_simulation_sync
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


def _forecast_dates(today: date, horizon: int = FORECAST_HORIZON_DAYS) -> list[date]:
    return [today + timedelta(days=h) for h in range(1, horizon + 1)]


def _forecast_is_fresh(plant_code: str, sim_date: date, max_age_hours: float) -> bool:
    """True if this date already has a current FORECAST run off recent-enough weather."""
    from app.api.repository import get_blocks
    from app.schedule import anchor_age_hours

    with session_scope() as db:
        rows = get_blocks(db, plant_code, sim_date, DataMode.FORECAST.value)
        if not rows:
            return False
        age = anchor_age_hours(rows)
    return age is not None and age <= max_age_hours


def run_forecast_prefetch() -> None:
    """Refresh the FORECAST horizon while the provider's daily quota is still alive.

    Deliberately split out of run_daily_job: that job is pinned to a plant-local
    publication deadline (00:30 IST = 19:00 UTC) and the free Open-Meteo quota, shared
    across the host's egress IP, is reliably exhausted from ~07:00 UTC until it resets at
    00:00 UTC. Every horizon fetch at 19:00 UTC therefore returned 429 and silently fell
    back to stored weather, so the horizon froze at whatever the last successful night had
    cached and the day-ahead schedule coasted on it until it ran out.

    Idempotent: a date whose stored forecast is younger than
    FORECAST_PREFETCH_MAX_AGE_HOURS is skipped. Running this hourly across the prefetch
    window therefore costs one cheap query per date once it has succeeded, and a morning
    that is still rate-limited simply retries the next hour.
    """
    settings = get_settings()
    max_age = settings.FORECAST_PREFETCH_MAX_AGE_HOURS
    for plant_code, tz in _active_plants():
        today = datetime.now(ZoneInfo(tz)).date()
        refreshed, already_fresh, rate_limited, failed = 0, 0, [], []
        for sim_date in _forecast_dates(today):
            if _forecast_is_fresh(plant_code, sim_date, max_age):
                already_fresh += 1
                continue
            try:
                summary = run_simulation_sync(
                    plant_code, sim_date, DataMode.FORECAST,
                    triggered_by="forecast-prefetch", force_refetch=True,
                )
                # A 429 does not raise — the run falls back to stored weather and reports
                # OK. Without this check the job would claim a refresh it did not make.
                if summary.weather_from_cache:
                    rate_limited.append(sim_date.isoformat())
                else:
                    refreshed += 1
            except Exception as exc:  # noqa: BLE001 — one bad date must not stop the rest
                failed.append(f"{sim_date}: {exc}")
        if rate_limited or failed:
            logger.warning(
                "Forecast prefetch plant=%s: %d refreshed, %d already fresh, "
                "%d stale (provider rate-limited: %s), %d failed (%s) — retrying next hour",
                plant_code, refreshed, already_fresh, len(rate_limited),
                ", ".join(rate_limited) or "-", len(failed), "; ".join(failed) or "-",
            )
        else:
            logger.info(
                "Forecast prefetch plant=%s: %d refreshed, %d already fresh",
                plant_code, refreshed, already_fresh,
            )


def _record_schedule_failure(plant_code: str, sim_date: date, exc: Exception) -> None:
    """Persist a schedule miss so it stays investigable after the logs roll.

    A logger.warning alone was not enough: a missing day-ahead schedule is a gap in a
    published commitment, and on a free host with no log retention nothing recorded it.
    Seven consecutive nights of failures left no queryable trace anywhere.
    """
    try:
        with session_scope() as db:
            db.add(ErrorLog(
                context=f"ensure_schedule {plant_code} {sim_date}",
                message=str(exc),
            ))
    except Exception as log_exc:  # noqa: BLE001 — recording a failure must never raise
        logger.error(
            "could not persist schedule failure plant=%s date=%s: %s",
            plant_code, sim_date, log_exc,
        )


def _issue_schedules(plant_code: str, today: date, settings: Settings) -> None:
    """Issue the day-ahead P90 schedules for today..today+SCHEDULE_HORIZON_DAYS.

    Isolated per date: a schedule failure must never affect the simulation steps, which
    are what the existing generation APIs serve.
    """
    from app.schedule import ensure_schedule

    for h in range(0, settings.SCHEDULE_HORIZON_DAYS + 1):
        sched_date = today + timedelta(days=h)
        try:
            ensure_schedule(plant_code, sched_date)
        except Exception as exc:  # noqa: BLE001 — never break the daily job
            logger.warning(
                "Schedule issue failed plant=%s date=%s: %s", plant_code, sched_date, exc
            )
            _record_schedule_failure(plant_code, sched_date, exc)


def run_daily_job() -> None:
    """Finalize yesterday, refresh today, and publish the day-ahead schedule.

    The FORECAST horizon is NOT fetched here — see run_forecast_prefetch for why this
    slot can never win the provider's quota race. When the prefetch job is disabled the
    horizon steps stay in this job so no capability is lost by the split.

    Every step is isolated: one failing date must not skip the rest. Sharing a single
    try/except here previously meant a rate-limited LIVE step silently cancelled the whole
    forecast horizon, so the horizon decayed to nothing while historical kept working.
    """
    logger.info("Daily job starting")
    settings = get_settings()
    for plant_code, tz in _active_plants():
        today = datetime.now(ZoneInfo(tz)).date()
        steps: list[tuple[date, DataMode]] = [
            (today - timedelta(days=1), DataMode.HISTORICAL),
            (today, DataMode.LIVE),
        ]
        if not settings.FORECAST_PREFETCH_ENABLED:
            steps += [(d, DataMode.FORECAST) for d in _forecast_dates(today)]
        failures: list[str] = []
        for sim_date, mode in steps:
            try:
                run_simulation_sync(
                    plant_code, sim_date, mode,
                    triggered_by="scheduler", force_refetch=True,
                )
            except Exception as exc:  # noqa: BLE001 — continue with the remaining steps
                failures.append(f"{sim_date} {mode.value}: {exc}")
                logger.error(
                    "Daily job step failed plant=%s date=%s mode=%s: %s",
                    plant_code, sim_date, mode.value, exc,
                )
        if settings.SCHEDULE_ENABLED:
            _issue_schedules(plant_code, today, settings)

        if failures:
            logger.error(
                "Daily job finished for plant=%s with %d/%d step(s) failed: %s",
                plant_code, len(failures), len(steps), "; ".join(failures),
            )
        else:
            logger.info("Daily job done for plant=%s", plant_code)


def run_schedule_retry() -> None:
    """Hourly self-heal for a schedule the daily job could not issue.

    The daily job gets exactly one attempt at its publication deadline. Before this job a
    single rate-limited night meant the date never got a schedule at all: the API served
    "No schedule issued" indefinitely and nothing ever retried. Idempotent — a published
    schedule is frozen, so in the normal case this is two indexed queries per date.
    """
    settings = get_settings()
    if not (settings.SCHEDULE_ENABLED and settings.SCHEDULE_RETRY_ENABLED):
        return
    from app.schedule import ensure_schedule, has_schedule

    for plant_code, tz in _active_plants():
        today = datetime.now(ZoneInfo(tz)).date()
        for h in range(0, settings.SCHEDULE_HORIZON_DAYS + 1):
            sched_date = today + timedelta(days=h)
            with session_scope() as db:
                if has_schedule(db, plant_code, sched_date):
                    continue
            try:
                result = ensure_schedule(plant_code, sched_date)
                if result.get("issued"):
                    logger.info(
                        "Schedule retry recovered plant=%s date=%s anchor=%s age=%sh",
                        plant_code, sched_date, result.get("anchor_mode"),
                        result.get("anchor_age_hours"),
                    )
            except Exception as exc:  # noqa: BLE001 — retry again next hour
                logger.warning(
                    "Schedule retry still failing plant=%s date=%s: %s",
                    plant_code, sched_date, exc,
                )


def run_live_refresh() -> None:
    """Keep today's LIVE simulation fresh for every active plant.

    Delegates to ensure_fresh_live rather than calling run_simulation_sync directly, so
    this job shares the same per-plant lock, freshness gate and provider backoff as
    consumer reads. Calling the simulation directly meant this job could collide with an
    in-flight consumer refresh (duplicate-key error on weather_block, since both
    delete-then-insert the same 96 rows) and that it kept calling a rate-limited provider
    while every other caller was correctly backing off.
    """
    for plant_code, _tz in _active_plants():
        try:
            result = ensure_fresh_live(plant_code)
            if result.get("error"):
                logger.error("Live refresh failed for plant=%s: %s", plant_code, result["error"])
            elif result.get("provider_backoff"):
                logger.info(
                    "Live refresh skipped for plant=%s: provider backoff, retry in %ss",
                    plant_code, result.get("retry_in_seconds"),
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
        if settings.FORECAST_PREFETCH_ENABLED:
            # Hourly across the prefetch window, in UTC — the provider quota resets at
            # 00:00 UTC, so this window is the only time the horizon can be refreshed.
            # The job is a no-op once the horizon is fresh, so the extra hours are just
            # free retries for a morning that is still rate-limited.
            self.scheduler.add_job(
                run_forecast_prefetch,
                CronTrigger(
                    hour=f"{settings.FORECAST_PREFETCH_START_HOUR_UTC}-"
                         f"{settings.FORECAST_PREFETCH_END_HOUR_UTC}",
                    minute=0,
                    timezone=ZoneInfo("UTC"),
                ),
                id="forecast_prefetch",
                replace_existing=True,
                misfire_grace_time=1800,
            )
        if settings.SCHEDULE_ENABLED and settings.SCHEDULE_RETRY_ENABLED:
            self.scheduler.add_job(
                run_schedule_retry,
                CronTrigger(minute=20, timezone=ZoneInfo("UTC")),
                id="schedule_retry",
                replace_existing=True,
                misfire_grace_time=600,
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
            "Scheduler started (daily=%s %s, live every %dmin, forecast prefetch=%s "
            "%02d:00-%02d:00 UTC, schedule retry=%s, keepalive=%s)",
            settings.SCHEDULER_DAILY_TIME, tz, settings.LIVE_REFRESH_MINUTES,
            settings.FORECAST_PREFETCH_ENABLED,
            settings.FORECAST_PREFETCH_START_HOUR_UTC,
            settings.FORECAST_PREFETCH_END_HOUR_UTC,
            settings.SCHEDULE_ENABLED and settings.SCHEDULE_RETRY_ENABLED,
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
