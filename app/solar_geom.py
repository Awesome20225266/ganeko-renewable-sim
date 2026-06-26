"""Solar geometry — sun elevation from date/time + location (NOAA algorithm).

Used to physically guarantee zero solar generation when the sun is below the horizon,
independent of any weather-data quirks (interpolation, partial days, bad timezones).
No external dependencies.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

# Standard sunrise/sunset altitude including atmospheric refraction.
HORIZON_DEG = -0.833


def sun_elevation_deg(dt_utc: datetime, lat: float, lon: float) -> float:
    """Solar elevation angle (degrees) for a UTC instant at (lat, lon).

    lon is positive east. Accurate to a fraction of a degree — ample for a
    day/night gate. Based on the NOAA solar-position equations.
    """
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=UTC)
    dt_utc = dt_utc.astimezone(UTC)

    day_of_year = dt_utc.timetuple().tm_yday
    hour = dt_utc.hour + dt_utc.minute / 60.0 + dt_utc.second / 3600.0

    # Fractional year (radians).
    gamma = 2.0 * math.pi / 365.0 * (day_of_year - 1 + (hour - 12.0) / 24.0)

    # Declination (radians).
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )

    # Equation of time (minutes).
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )

    # True solar time (minutes), then hour angle (degrees).
    time_offset = eqtime + 4.0 * lon  # lon east positive; UTC reference
    tst = (hour * 60.0) + time_offset
    ha = (tst / 4.0) - 180.0

    lat_r = math.radians(lat)
    ha_r = math.radians(ha)
    cos_zenith = math.sin(lat_r) * math.sin(decl) + math.cos(lat_r) * math.cos(decl) * math.cos(ha_r)
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith = math.acos(cos_zenith)
    return 90.0 - math.degrees(zenith)


def is_daylight(local_naive_dt: datetime, tz: str, lat: float, lon: float) -> bool:
    """True if the sun is above the (refraction-adjusted) horizon at a local block time."""
    aware = local_naive_dt.replace(tzinfo=ZoneInfo(tz))
    return sun_elevation_deg(aware, lat, lon) > HORIZON_DEG
