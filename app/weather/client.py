"""Async Open-Meteo HTTP client.

Selects the correct endpoint per simulation mode, fetches live data with
retry/backoff and HTTP-429 handling, and returns the raw response verbatim
(with request URL + fetch timestamp) for persistence. No API key is required.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

import httpx

from app.config.settings import Settings, get_settings
from app.logging_conf import get_logger

logger = get_logger(__name__)


class DataMode(str, Enum):
    HISTORICAL = "HISTORICAL"
    LIVE = "LIVE"
    FORECAST = "FORECAST"


# Variable plans -------------------------------------------------------------
# Hourly variables are always available across forecast / historical-forecast.
HOURLY_SOLAR = [
    "shortwave_radiation",
    "direct_radiation",
    "diffuse_radiation",
    "direct_normal_irradiance",
    "temperature_2m",
    "cloud_cover",
    "is_day",
]
HOURLY_WIND = [
    "wind_speed_10m",
    "wind_speed_100m",
    "wind_speed_120m",
    "wind_speed_180m",
    "wind_direction_100m",
    "wind_gusts_10m",
    "surface_pressure",
]
GTI_VAR = "global_tilted_irradiance"

# minutely_15 supports a subset (native 15-min, Europe/NA only; elsewhere interpolated).
MINUTELY15_VARS = [
    "shortwave_radiation",
    "direct_radiation",
    "diffuse_radiation",
    "direct_normal_irradiance",
    "temperature_2m",
    "wind_speed_10m",
    "wind_speed_80m",
    "wind_speed_120m",
    "wind_speed_180m",
    "wind_gusts_10m",
    "is_day",
]

# ERA5 archive: hourly only, no is_day / GTI / 120m / 180m.
ARCHIVE_HOURLY = [
    "shortwave_radiation",
    "direct_radiation",
    "diffuse_radiation",
    "direct_normal_irradiance",
    "temperature_2m",
    "cloud_cover",
    "wind_speed_10m",
    "wind_speed_100m",
    "wind_direction_100m",
    "wind_gusts_10m",
    "surface_pressure",
]

# historical-forecast API reliably covers the recent past (~last 2 years); older
# dates fall back to the ERA5 archive (reanalysis).
HISTORICAL_FORECAST_MAX_AGE_DAYS = 730
# Both archive and historical-forecast lag real time by a few days.
ARCHIVE_DELAY_DAYS = 5


@dataclass
class RawFetch:
    plant_code: str
    sim_date: date
    mode: DataMode
    provider: str
    weather_source: str  # endpoint label, e.g. "open-meteo:forecast"
    request_url: str
    params: dict[str, Any]
    fetched_at: datetime
    json: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlantLike:
    """Minimal plant view the client needs (decouples from ORM)."""

    plant_code: str
    latitude: float
    longitude: float
    timezone: str
    panel_tilt: float = 0.0
    panel_azimuth: float = 180.0
    use_global_tilted_irradiance: bool = False


def _today_local(tz: str) -> date:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(tz)).date()


def select_request(
    plant: PlantLike,
    sim_date: date,
    mode: DataMode,
    settings: Settings | None = None,
    today: date | None = None,
) -> tuple[str, dict[str, Any], str]:
    """Return (url, params, weather_source) for the given mode/date.

    Endpoint rules:
      FORECAST   -> forecast API, start_date=end_date=sim_date (<= 16 days out).
      LIVE       -> forecast API with past_days=1 & forecast_days=2 (covers today).
      HISTORICAL -> historical-forecast API if within ~2 years & past the archive lag,
                    else the ERA5 archive (hourly).
    """
    settings = settings or get_settings()
    today = today or _today_local(plant.timezone)

    base = {
        "latitude": plant.latitude,
        "longitude": plant.longitude,
        "timezone": plant.timezone,
        # Wind speed/gusts in m/s (Open-Meteo defaults to km/h) for the power curve;
        # temperature in °C and radiation in W/m² are already the defaults.
        "wind_speed_unit": "ms",
    }

    if mode == DataMode.FORECAST:
        hourly = HOURLY_SOLAR + HOURLY_WIND
        m15 = list(MINUTELY15_VARS)
        if plant.use_global_tilted_irradiance:
            hourly = hourly + [GTI_VAR]
            m15 = m15 + [GTI_VAR]
            base["tilt"] = plant.panel_tilt
            base["azimuth"] = plant.panel_azimuth
        params = {
            **base,
            "hourly": ",".join(hourly),
            "minutely_15": ",".join(m15),
            "start_date": sim_date.isoformat(),
            "end_date": sim_date.isoformat(),
        }
        return settings.OPEN_METEO_FORECAST_URL, params, "open-meteo:forecast"

    if mode == DataMode.LIVE:
        hourly = HOURLY_SOLAR + HOURLY_WIND
        m15 = list(MINUTELY15_VARS)
        if plant.use_global_tilted_irradiance:
            hourly = hourly + [GTI_VAR]
            m15 = m15 + [GTI_VAR]
            base["tilt"] = plant.panel_tilt
            base["azimuth"] = plant.panel_azimuth
        params = {
            **base,
            "hourly": ",".join(hourly),
            "minutely_15": ",".join(m15),
            "past_days": 1,
            "forecast_days": 2,
        }
        return settings.OPEN_METEO_FORECAST_URL, params, "open-meteo:forecast(live)"

    # HISTORICAL
    age_days = (today - sim_date).days
    use_archive = age_days > HISTORICAL_FORECAST_MAX_AGE_DAYS
    if use_archive:
        params = {
            **base,
            "hourly": ",".join(ARCHIVE_HOURLY),
            "start_date": sim_date.isoformat(),
            "end_date": sim_date.isoformat(),
        }
        return settings.OPEN_METEO_ARCHIVE_URL, params, "open-meteo:archive(era5)"

    hourly = HOURLY_SOLAR + HOURLY_WIND
    m15 = list(MINUTELY15_VARS)
    if plant.use_global_tilted_irradiance:
        hourly = hourly + [GTI_VAR]
        m15 = m15 + [GTI_VAR]
        base["tilt"] = plant.panel_tilt
        base["azimuth"] = plant.panel_azimuth
    params = {
        **base,
        "hourly": ",".join(hourly),
        "minutely_15": ",".join(m15),
        "start_date": sim_date.isoformat(),
        "end_date": sim_date.isoformat(),
    }
    return (
        settings.OPEN_METEO_HISTORICAL_FORECAST_URL,
        params,
        "open-meteo:historical-forecast",
    )


async def _get_with_retry(
    client: httpx.AsyncClient, url: str, params: dict[str, Any], max_retries: int
) -> httpx.Response:
    delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = await client.get(url, params=params)
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
                logger.warning(
                    "Open-Meteo %s (attempt %d/%d); backing off %.1fs",
                    resp.status_code, attempt, max_retries, wait,
                )
                await asyncio.sleep(wait)
                delay = min(delay * 2, 30.0)
                last_exc = httpx.HTTPStatusError(
                    f"status {resp.status_code}", request=resp.request, response=resp
                )
                continue
            resp.raise_for_status()
            return resp
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            logger.warning(
                "Open-Meteo transport error (attempt %d/%d): %s; backing off %.1fs",
                attempt, max_retries, exc, delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
    raise RuntimeError(f"Open-Meteo request failed after {max_retries} attempts") from last_exc


async def fetch_weather(
    plant: PlantLike,
    sim_date: date,
    mode: DataMode,
    settings: Settings | None = None,
    today: date | None = None,
) -> RawFetch:
    """Fetch raw weather JSON from the live Open-Meteo API for one plant/date/mode."""
    settings = settings or get_settings()
    url, params, source = select_request(plant, sim_date, mode, settings, today)
    timeout = httpx.Timeout(settings.OPEN_METEO_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "renewable-sim/1.0"}) as client:
        resp = await _get_with_retry(client, url, params, settings.OPEN_METEO_MAX_RETRIES)
        data = resp.json()
    fetched_at = datetime.now(UTC)
    logger.info(
        "Fetched weather plant=%s date=%s mode=%s source=%s status=ok",
        plant.plant_code, sim_date, mode.value, source,
    )
    return RawFetch(
        plant_code=plant.plant_code,
        sim_date=sim_date,
        mode=mode,
        provider="open-meteo",
        weather_source=source,
        request_url=str(resp.url),
        params=params,
        fetched_at=fetched_at,
        json=data,
    )


def resolve_mode(sim_date: date, tz: str) -> DataMode:
    """Pick the natural mode for a date relative to the plant's local 'today'."""
    today = _today_local(tz)
    if sim_date < today:
        return DataMode.HISTORICAL
    if sim_date == today:
        return DataMode.LIVE
    return DataMode.FORECAST
