"""Persistence for raw weather responses and normalized 15-minute weather blocks."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import RawWeatherResponse, WeatherBlock
from app.logging_conf import get_logger
from app.weather.client import DataMode, RawFetch
from app.weather.normalize import NormalizedBlock

logger = get_logger(__name__)

# A response is only reusable for a date if its time axis actually spans that whole
# local day. Hourly-only responses end at 23:00 (the 23:00->23:45 tail is interpolated
# for every day the normal way), so that is the bar — anything shorter is rejected,
# because normalize() would otherwise flat-fill the gap from the nearest edge sample.
_LAST_REQUIRED_SAMPLE = time(23, 0)


def find_cached_raw(
    db: Session, plant_code: str, sim_date: date, mode: DataMode
) -> RawWeatherResponse | None:
    """Most recent raw response for plant+date+mode (for cache/avoid-duplicate-fetch)."""
    return db.scalars(
        select(RawWeatherResponse)
        .where(
            RawWeatherResponse.plant_code == plant_code,
            RawWeatherResponse.sim_date == sim_date,
            RawWeatherResponse.data_mode == mode.value,
        )
        .order_by(RawWeatherResponse.fetched_at.desc())
    ).first()


def covers_date(raw_json: dict, sim_date: date) -> bool:
    """True if this provider response's time axis spans the whole local day `sim_date`."""
    need_first = datetime.combine(sim_date, time.min)
    need_last = datetime.combine(sim_date, _LAST_REQUIRED_SAMPLE)
    for section in ("minutely_15", "hourly"):
        times = ((raw_json or {}).get(section) or {}).get("time") or []
        if not times:
            continue
        try:
            first = datetime.fromisoformat(str(times[0]))
            last = datetime.fromisoformat(str(times[-1]))
        except (TypeError, ValueError):
            continue
        if first <= need_first and last >= need_last:
            return True
    return False


def find_raw_covering(
    db: Session, plant_code: str, sim_date: date, lookback_days: int = 2
) -> RawWeatherResponse | None:
    """Most recent stored response that fully covers `sim_date`, whatever it was fetched for.

    This is the offline fallback for when the provider is unreachable or rate-limiting
    us. A LIVE request uses past_days=1&forecast_days=2, so last night's response already
    contains today (and tomorrow) — reusing it costs zero provider calls and keeps a day
    from having no data at all. Responses that don't span the full day are skipped.
    """
    rows = db.scalars(
        select(RawWeatherResponse)
        .where(
            RawWeatherResponse.plant_code == plant_code,
            RawWeatherResponse.sim_date >= sim_date - timedelta(days=lookback_days),
            RawWeatherResponse.sim_date <= sim_date + timedelta(days=lookback_days),
        )
        .order_by(RawWeatherResponse.fetched_at.desc())
        .limit(40)
    )
    for row in rows:
        if covers_date(row.raw_json, sim_date):
            return row
    logger.warning(
        "No stored weather response covers plant=%s date=%s (looked back %d days)",
        plant_code, sim_date, lookback_days,
    )
    return None


def persist_raw(db: Session, fetch: RawFetch) -> RawWeatherResponse:
    """Store the raw JSON verbatim with request URL + fetch timestamp (kept as history)."""
    row = RawWeatherResponse(
        plant_code=fetch.plant_code,
        sim_date=fetch.sim_date,
        data_mode=fetch.mode.value,
        provider=fetch.provider,
        request_url=fetch.request_url,
        fetched_at=fetch.fetched_at,
        raw_json=fetch.json,
    )
    db.add(row)
    db.flush()
    return row


def persist_weather_blocks(
    db: Session,
    plant_code: str,
    sim_date: date,
    mode: DataMode,
    weather_source: str,
    blocks: list[NormalizedBlock],
    fetched_at,
) -> int:
    """Replace normalized weather for plant+date+mode with the given 96 blocks."""
    db.query(WeatherBlock).filter(
        WeatherBlock.plant_code == plant_code,
        WeatherBlock.sim_date == sim_date,
        WeatherBlock.data_mode == mode.value,
    ).delete(synchronize_session=False)

    for b in blocks:
        db.add(
            WeatherBlock(
                plant_code=plant_code,
                sim_date=sim_date,
                block_no=b.block_no,
                block_start=b.block_start,
                block_end=b.block_end,
                data_mode=mode.value,
                weather_source=weather_source,
                fetched_at=fetched_at,
                interpolated=b.interpolated,
                ghi=b.ghi,
                poa=b.poa,
                dni=b.dni,
                dhi=b.dhi,
                temperature_2m=b.temperature_2m,
                cloud_cover=b.cloud_cover,
                is_day=b.is_day,
                wind_speed_10m=b.wind_speed_10m,
                wind_speed_100m=b.wind_speed_100m,
                wind_speed_120m=b.wind_speed_120m,
                wind_speed_180m=b.wind_speed_180m,
                wind_direction_100m=b.wind_direction_100m,
                wind_gusts_10m=b.wind_gusts_10m,
                surface_pressure=b.surface_pressure,
            )
        )
    db.flush()
    return len(blocks)
