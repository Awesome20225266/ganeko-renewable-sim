"""Persistence for raw weather responses and normalized 15-minute weather blocks."""
from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import RawWeatherResponse, WeatherBlock
from app.weather.client import DataMode, RawFetch
from app.weather.normalize import NormalizedBlock


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
