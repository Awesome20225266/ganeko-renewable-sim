"""Guards for the two failure modes that let a day-ahead schedule go quietly wrong.

Both are regressions from a real incident. For seven consecutive nights the forecast
step hit Open-Meteo's rate limit, fell back to week-old stored weather, and reported OK —
so `ensure_schedule` kept publishing schedules anchored on a forecast that no longer
described the day, and every health signal stayed green until the stored horizon ran out
and the day-ahead schedule simply vanished.

  1. A stale anchor must be refused, not published (SCHEDULE_MAX_ANCHOR_AGE_HOURS).
  2. For sim_date == today, a genuine day-ahead FORECAST anchor must beat that morning's
     LIVE run, so a recovery attempt at 00:30 local time cannot silently relabel a
     day-ahead commitment as anchor_mode=LIVE.
"""
from __future__ import annotations

import math
import os
import tempfile
from datetime import UTC, date, datetime, timedelta

# Isolated test DB BEFORE importing app modules.
_TMPDIR = tempfile.mkdtemp(prefix="rensim_anchor_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDIR}/test.db".replace("\\", "/")
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["ADMIN_BOOTSTRAP_KEY"] = "test-admin-key"
os.environ["PLANT_CODE"] = "HYBRID01"

import pytest  # noqa: E402

import app.config.settings as settings_mod  # noqa: E402
import app.db.base as db_base  # noqa: E402

settings_mod.get_settings.cache_clear()
db_base._engine = None
db_base._SessionLocal = None

from app.db.base import session_scope  # noqa: E402
from app.db.models import GenerationBlock, ScheduleBlock, WeatherBlock  # noqa: E402
from app.db.seed import run_seed  # noqa: E402
from app.schedule import (  # noqa: E402
    ScheduleError,
    anchor_age_hours,
    ensure_schedule,
    has_schedule,
)

PLANT = "HYBRID01"


def _seed(sim_date: date, mode: str, weather_age_hours: float | None) -> None:
    """Seed one shaped day of generation + weather with a specific weather age.

    `weather_age_hours=None` leaves weather_fetch_time NULL, which is how pre-existing
    rows look — the guard must treat an unknown age as usable rather than reject it.
    """
    fetch_time = (
        None if weather_age_hours is None
        else datetime.now(UTC) - timedelta(hours=weather_age_hours)
    )
    with session_scope() as db:
        for n in range(1, 97):
            hour = (n - 1) * 0.25
            solar = (160.0 * max(0.0, math.sin(math.pi * (hour - 6) / 12))
                     if 6 <= hour <= 18 else 0.0)
            wind = 40.0 + 25.0 * math.sin(2 * math.pi * hour / 24)
            start = (datetime.combine(sim_date, datetime.min.time())
                     + timedelta(minutes=15 * (n - 1)))
            db.add(GenerationBlock(
                plant_code=PLANT, sim_date=sim_date, block_no=n,
                block_start=start, block_end=start + timedelta(minutes=15),
                solar_mw=solar, solar_mwh=solar * 0.25,
                wind_mw=wind, wind_mwh=wind * 0.25,
                total_mw=solar + wind, total_mwh=(solar + wind) * 0.25,
                solar_cuf=0.0, wind_cuf=0.0, hybrid_cuf=0.0,
                solar_status="OK", wind_status="OK",
                data_mode=mode, data_source="test", data_label=f"{mode}_SIMULATED",
                data_quality_status="OK", simulation_version="v1.0.0",
                model_assumption_version="v1.0.0", plant_config_version=1,
                weather_source="test", weather_fetch_time=fetch_time, is_current=True,
            ))
            db.add(WeatherBlock(
                plant_code=PLANT, sim_date=sim_date, block_no=n,
                block_start=start, block_end=start + timedelta(minutes=15),
                data_mode=mode, weather_source="test", interpolated=False,
                cloud_cover=35.0, wind_speed_100m=8.0, fetched_at=fetch_time,
            ))


def _clear(sim_date: date) -> None:
    with session_scope() as db:
        db.query(ScheduleBlock).filter(ScheduleBlock.sim_date == sim_date).delete()
        db.query(GenerationBlock).filter(GenerationBlock.sim_date == sim_date).delete()
        db.query(WeatherBlock).filter(WeatherBlock.sim_date == sim_date).delete()


@pytest.fixture(scope="module", autouse=True)
def _seeded():
    run_seed()
    yield


# --- anchor_age_hours -------------------------------------------------------
def test_age_reads_weather_fetch_time_not_processed_at():
    """A stale re-simulation stamps a new processed_at; the weather age must not move."""
    d = date.today() + timedelta(days=40)
    _clear(d)
    _seed(d, "FORECAST", weather_age_hours=100)
    with session_scope() as db:
        rows = db.query(GenerationBlock).filter(GenerationBlock.sim_date == d).all()
        age = anchor_age_hours(rows)
    assert age == pytest.approx(100, abs=1)


def test_unknown_age_is_not_treated_as_stale():
    """Legacy rows have no weather_fetch_time — they must stay publishable."""
    d = date.today() + timedelta(days=41)
    _clear(d)
    _seed(d, "FORECAST", weather_age_hours=None)
    with session_scope() as db:
        rows = db.query(GenerationBlock).filter(GenerationBlock.sim_date == d).all()
        assert anchor_age_hours(rows) is None
    assert ensure_schedule(PLANT, d)["issued"] is True


# --- staleness guard --------------------------------------------------------
def test_stale_anchor_is_refused_and_nothing_is_published():
    """The incident case: only a week-old forecast exists for a future date."""
    d = date.today() + timedelta(days=42)
    _clear(d)
    _seed(d, "FORECAST", weather_age_hours=24 * 7)

    with pytest.raises(ScheduleError) as exc:
        ensure_schedule(PLANT, d)
    msg = str(exc.value)
    assert "stale" in msg.lower()
    assert "FORECAST" in msg and "168h" in msg

    with session_scope() as db:
        assert has_schedule(db, PLANT, d) is False


def test_fresh_anchor_publishes_and_reports_its_age():
    d = date.today() + timedelta(days=43)
    _clear(d)
    _seed(d, "FORECAST", weather_age_hours=18)

    res = ensure_schedule(PLANT, d)
    assert res["issued"] is True
    assert res["anchor_mode"] == "FORECAST"
    assert res["anchor_age_hours"] == pytest.approx(18, abs=1)


def test_stale_candidate_is_skipped_so_a_fresh_one_can_win():
    """A stale FORECAST must not block a fresh LIVE anchor for the same date."""
    d = date.today()
    _clear(d)
    _seed(d, "FORECAST", weather_age_hours=24 * 6)
    _seed(d, "LIVE", weather_age_hours=1)

    res = ensure_schedule(PLANT, d, force=True)
    assert res["issued"] is True
    assert res["anchor_mode"] == "LIVE"


# --- anchor order for sim_date == today -------------------------------------
def test_day_ahead_forecast_anchor_beats_same_day_live_run():
    """The bug that would have mislabelled 2026-08-25.

    Recovering today's missing schedule at 00:30 local time runs after the daily job's
    LIVE step, so with the old `sim_date > today` test the LIVE run won and the schedule
    was frozen with anchor_mode=LIVE — a same-day anchor served as a day-ahead
    commitment. Both anchors are fresh here, so only preference order decides.
    """
    d = date.today()
    _clear(d)
    _seed(d, "LIVE", weather_age_hours=1)
    _seed(d, "FORECAST", weather_age_hours=20)

    res = ensure_schedule(PLANT, d, force=True)
    assert res["issued"] is True
    assert res["anchor_mode"] == "FORECAST"


def test_past_dates_still_prefer_actuals_over_forecast():
    """Regression guard: the order flip must not touch historical dates."""
    d = date.today() - timedelta(days=5)
    _clear(d)
    _seed(d, "FORECAST", weather_age_hours=2)
    _seed(d, "HISTORICAL", weather_age_hours=2)

    res = ensure_schedule(PLANT, d, force=True)
    assert res["anchor_mode"] == "HISTORICAL"


# --- freeze semantics still hold -------------------------------------------
def test_published_schedule_stays_frozen_even_if_anchor_goes_stale():
    """A published commitment must never be withdrawn by the new guard."""
    d = date.today() + timedelta(days=44)
    _clear(d)
    _seed(d, "FORECAST", weather_age_hours=2)
    assert ensure_schedule(PLANT, d)["issued"] is True

    # Age the anchor past the limit, then re-run the way the retry job would.
    with session_scope() as db:
        db.query(GenerationBlock).filter(GenerationBlock.sim_date == d).update(
            {GenerationBlock.weather_fetch_time: datetime.now(UTC) - timedelta(days=9)}
        )
    res = ensure_schedule(PLANT, d)
    assert res["issued"] is False and res["reason"] == "frozen"
    with session_scope() as db:
        assert has_schedule(db, PLANT, d) is True
