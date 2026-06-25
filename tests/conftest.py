"""Shared test fixtures: a deterministic plant spec and synthetic weather blocks."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from app.db.seed import WIND_POWER_CURVE, WIND_RATED_KW
from app.engines.spec import PlantSpec
from app.weather.normalize import NormalizedBlock


@pytest.fixture
def spec() -> PlantSpec:
    return PlantSpec(
        plant_code="TEST01",
        plant_name="Test Plant",
        latitude=26.9,
        longitude=70.9,
        timezone="Asia/Kolkata",
        config_version=1,
        solar_ac_mw=160.0,
        solar_dc_mw=240.0,
        dc_ac_ratio=1.5,
        solar_performance_ratio=0.80,
        solar_loss_factor=0.10,
        temp_coeff_pct_per_c=-0.40,
        panel_tilt=25.0,
        panel_azimuth=180.0,
        use_global_tilted_irradiance=False,
        wind_ac_mw=135.0,
        wind_loss_factor=0.08,
        wind_power_curve=WIND_POWER_CURVE,
        curve_rated_kw=WIND_RATED_KW,
        hub_height_m=100.0,
        cut_in_ms=3.0,
        rated_ms=12.0,
        cut_out_ms=25.0,
        air_density_correction=False,
        block_minutes=15,
    )


def make_block(
    block_no: int,
    poa: float = 0.0,
    is_day: int = 0,
    temp: float = 25.0,
    ws100: float | None = 7.0,
    interpolated: bool = False,
    ws10: float | None = None,
) -> NormalizedBlock:
    base = datetime(2026, 6, 1)
    start = base + timedelta(minutes=15 * (block_no - 1))
    return NormalizedBlock(
        block_no=block_no,
        block_start=start,
        block_end=start + timedelta(minutes=15),
        interpolated=interpolated,
        ghi=poa,
        poa=poa,
        dni=poa,
        dhi=0.0,
        temperature_2m=temp,
        cloud_cover=0.0,
        is_day=is_day,
        wind_speed_10m=ws10,
        wind_speed_100m=ws100,
        wind_speed_120m=None,
        wind_speed_180m=None,
        wind_direction_100m=180.0,
        wind_gusts_10m=None,
        surface_pressure=950.0,
    )


@pytest.fixture
def synthetic_day(spec):
    """A physically-shaped clear day: half-sine solar (sunrise~06:00, sunset~18:00)."""
    blocks = []
    for n in range(1, 97):
        minutes = (n - 1) * 15
        hour = minutes / 60.0
        if 6.0 <= hour <= 18.0:
            poa = 1000.0 * max(0.0, math.sin(math.pi * (hour - 6.0) / 12.0))
            is_day = 1
        else:
            poa = 0.0
            is_day = 0
        # Wind varies smoothly 4..10 m/s.
        ws = 7.0 + 3.0 * math.sin(2 * math.pi * hour / 24.0)
        blocks.append(make_block(n, poa=poa, is_day=is_day, temp=30.0, ws100=ws))
    return blocks
