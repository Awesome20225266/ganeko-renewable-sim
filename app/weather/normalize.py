"""Normalize a raw Open-Meteo response into exactly 96 clean 15-minute blocks.

Robustly resamples/interpolates hourly data to the 15-min grid (native minutely_15
is only available in Central Europe & North America; elsewhere — and for the ERA5
archive — data is hourly). Each block carries an `interpolated` flag indicating it was
synthesized between native samples.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

from app.solar_geom import is_daylight
from app.weather.client import GTI_VAR

# Output weather variables and how to interpolate them from hourly data.
_LINEAR_VARS = [
    "shortwave_radiation",
    "direct_radiation",
    "diffuse_radiation",
    "direct_normal_irradiance",
    GTI_VAR,
    "temperature_2m",
    "cloud_cover",
    "wind_speed_10m",
    "wind_speed_100m",
    "wind_speed_120m",
    "wind_speed_180m",
    "wind_speed_80m",
    "wind_direction_100m",
    "wind_gusts_10m",
    "surface_pressure",
]
_RADIATION_VARS = {
    "shortwave_radiation",
    "direct_radiation",
    "diffuse_radiation",
    "direct_normal_irradiance",
    GTI_VAR,
}


@dataclass
class NormalizedBlock:
    block_no: int  # 1..96
    block_start: datetime  # naive local
    block_end: datetime
    interpolated: bool
    ghi: float | None = None
    poa: float | None = None
    dni: float | None = None
    dhi: float | None = None
    temperature_2m: float | None = None
    cloud_cover: float | None = None
    is_day: int | None = None
    wind_speed_10m: float | None = None
    wind_speed_100m: float | None = None
    wind_speed_120m: float | None = None
    wind_speed_180m: float | None = None
    wind_direction_100m: float | None = None
    wind_gusts_10m: float | None = None
    surface_pressure: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _to_frame(section: dict[str, Any] | None) -> pd.DataFrame | None:
    """Build a time-indexed DataFrame from an Open-Meteo 'hourly'/'minutely_15' block."""
    if not section or "time" not in section or not section["time"]:
        return None
    times = pd.to_datetime(section["time"])
    data = {}
    for key, values in section.items():
        if key == "time":
            continue
        data[key] = pd.to_numeric(pd.Series(values, dtype="object"), errors="coerce")
    df = pd.DataFrame(data)
    df.index = times
    df = df[~df.index.duplicated(keep="first")].sort_index()
    return df


def _interp_linear(series: pd.Series, target: pd.DatetimeIndex) -> pd.Series:
    s = series.dropna()
    if s.empty:
        return pd.Series(np.nan, index=target)
    union = s.index.union(target)
    out = s.reindex(union).interpolate(method="time", limit_direction="both")
    out = out.reindex(target).ffill().bfill()
    return out


def _step(series: pd.Series, target: pd.DatetimeIndex) -> pd.Series:
    s = series.dropna()
    if s.empty:
        return pd.Series(np.nan, index=target)
    union = s.index.union(target)
    out = s.reindex(union).ffill().bfill()
    return out.reindex(target)


def normalize_to_blocks(
    raw_json: dict[str, Any],
    sim_date: date,
    use_global_tilted_irradiance: bool = False,
    latitude: float | None = None,
    longitude: float | None = None,
    timezone: str | None = None,
) -> list[NormalizedBlock]:
    """Return exactly 96 NormalizedBlock objects for `sim_date` (plant-local time).

    If latitude/longitude/timezone are given, a physical sun-elevation gate forces
    POA = 0 and is_day = 0 for any block where the sun is below the horizon — so no
    weather-data quirk can ever produce solar generation at night.
    """
    hourly_df = _to_frame(raw_json.get("hourly"))
    m15_df = _to_frame(raw_json.get("minutely_15"))

    start = pd.Timestamp(sim_date)
    target = pd.date_range(start=start, periods=96, freq="15min")

    def native_then_interp(name: str, step: bool = False) -> tuple[pd.Series, list[str]]:
        # Prefer native minutely_15 exact matches; fall back to hourly interpolation.
        m = (
            m15_df[name].reindex(target)
            if (m15_df is not None and name in m15_df.columns)
            else pd.Series(np.nan, index=target)
        )
        if hourly_df is not None and name in hourly_df.columns:
            h = _step(hourly_df[name], target) if step else _interp_linear(hourly_df[name], target)
        else:
            h = pd.Series(np.nan, index=target)
        vals = []
        src = []
        for t in target:
            mv = m.loc[t]
            if not pd.isna(mv):
                vals.append(float(mv))
                src.append("m15")
            elif not pd.isna(h.loc[t]):
                vals.append(float(h.loc[t]))
                src.append("hourly")
            else:
                vals.append(np.nan)
                src.append("none")
        return pd.Series(vals, index=target), src

    cols: dict[str, pd.Series] = {}
    srcs: dict[str, list[str]] = {}
    for name in _LINEAR_VARS:
        s, src = native_then_interp(name)
        cols[name] = s
        srcs[name] = src

    # is_day: step function; if absent derive from GHI later.
    if (hourly_df is not None and "is_day" in hourly_df.columns) or (
        m15_df is not None and "is_day" in m15_df.columns
    ):
        is_day_series, _ = native_then_interp("is_day", step=True)
    else:
        is_day_series = pd.Series(np.nan, index=target)

    # Clamp radiation to >= 0 and treat any missing radiation as 0 (no data -> no
    # irradiance), never carried over from daytime via fill.
    for name in _RADIATION_VARS:
        if name in cols:
            cols[name] = cols[name].clip(lower=0.0).fillna(0.0)

    ghi = cols.get("shortwave_radiation", pd.Series(np.nan, index=target))
    gti = cols.get(GTI_VAR, pd.Series(np.nan, index=target))

    use_geo = latitude is not None and longitude is not None and timezone is not None

    blocks: list[NormalizedBlock] = []
    for i, t in enumerate(target):
        block_no = i + 1
        # Provenance: interpolated unless this block came from a native 15-min sample
        # OR sits exactly on a native hourly sample (minute == 0 with hourly source).
        ghi_src = srcs.get("shortwave_radiation", ["none"] * 96)[i]
        if ghi_src == "m15":
            interpolated = False
        elif ghi_src == "hourly":
            interpolated = t.minute != 0
        else:
            interpolated = True

        ghi_v = float(ghi.iloc[i]) if not pd.isna(ghi.iloc[i]) else 0.0
        gti_v = float(gti.iloc[i]) if not pd.isna(gti.iloc[i]) else None
        if use_global_tilted_irradiance and gti_v is not None:
            poa_v = max(0.0, gti_v)
        else:
            poa_v = max(0.0, ghi_v)

        # is_day: use provided, else derive from POA/GHI.
        idv = is_day_series.iloc[i]
        if pd.isna(idv):
            is_day_v = 1 if ghi_v > 5.0 else 0
        else:
            is_day_v = int(round(float(idv)))
        # Enforce physical consistency: no daylight when there is no irradiance.
        if ghi_v <= 0.0 and poa_v <= 0.0:
            is_day_v = 0

        # Physical sun-elevation gate: if the sun is below the horizon, force night —
        # zero irradiance regardless of any weather-data quirk. This makes night-time
        # solar generation impossible.
        block_night = False
        if use_geo:
            mid = (t + pd.Timedelta(minutes=7, seconds=30)).to_pydatetime()
            if not is_daylight(mid, timezone, latitude, longitude):
                block_night = True
                ghi_v = 0.0
                gti_v = 0.0
                poa_v = 0.0
                is_day_v = 0

        def cell(name: str, i: int = i, night: bool = block_night) -> float | None:
            s = cols.get(name)
            if s is None:
                return None
            v = s.iloc[i]
            if pd.isna(v):
                return None
            if night and name in _RADIATION_VARS:
                return 0.0
            return float(v)

        blocks.append(
            NormalizedBlock(
                block_no=block_no,
                block_start=t.to_pydatetime(),
                block_end=(t + pd.Timedelta(minutes=15)).to_pydatetime(),
                interpolated=bool(interpolated),
                ghi=ghi_v,
                poa=poa_v,
                dni=cell("direct_normal_irradiance"),
                dhi=cell("diffuse_radiation"),
                temperature_2m=cell("temperature_2m"),
                cloud_cover=cell("cloud_cover"),
                is_day=is_day_v,
                wind_speed_10m=cell("wind_speed_10m"),
                wind_speed_100m=cell("wind_speed_100m"),
                wind_speed_120m=cell("wind_speed_120m"),
                wind_speed_180m=cell("wind_speed_180m"),
                wind_direction_100m=cell("wind_direction_100m"),
                wind_gusts_10m=cell("wind_gusts_10m"),
                surface_pressure=cell("surface_pressure"),
                extra={"wind_speed_80m": cell("wind_speed_80m")},
            )
        )

    assert len(blocks) == 96, f"expected 96 blocks, got {len(blocks)}"
    return blocks
