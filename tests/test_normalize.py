"""Tests for hourly -> 15-minute normalization into a clean 96-block grid."""
from __future__ import annotations

import math
from datetime import date

from app.weather.normalize import normalize_to_blocks


def _synthetic_hourly_raw(d: date) -> dict:
    times, ghi, temp, ws100, isday = [], [], [], [], []
    for h in range(24):
        times.append(f"{d.isoformat()}T{h:02d}:00")
        val = 900.0 * max(0.0, math.sin(math.pi * (h - 6) / 12)) if 6 <= h <= 18 else 0.0
        ghi.append(val)
        temp.append(25.0 + 5 * math.sin(math.pi * (h - 6) / 12))
        ws100.append(6.0 + 2 * math.sin(2 * math.pi * h / 24))
        isday.append(1 if 6 <= h <= 18 else 0)
    return {
        "hourly": {
            "time": times,
            "shortwave_radiation": ghi,
            "direct_radiation": ghi,
            "diffuse_radiation": [0.0] * 24,
            "direct_normal_irradiance": ghi,
            "temperature_2m": temp,
            "cloud_cover": [0.0] * 24,
            "is_day": isday,
            "wind_speed_10m": [w * 0.7 for w in ws100],
            "wind_speed_100m": ws100,
            "wind_speed_120m": ws100,
            "wind_speed_180m": ws100,
            "wind_direction_100m": [180.0] * 24,
            "wind_gusts_10m": ws100,
            "surface_pressure": [950.0] * 24,
        }
    }


def test_exactly_96_blocks_no_gaps():
    d = date(2026, 6, 1)
    blocks = normalize_to_blocks(_synthetic_hourly_raw(d), d)
    assert len(blocks) == 96
    assert [b.block_no for b in blocks] == list(range(1, 97))
    # 15-minute spacing, no duplicate timestamps.
    starts = [b.block_start for b in blocks]
    assert len(set(starts)) == 96
    for a, b in zip(blocks, blocks[1:]):
        assert (b.block_start - a.block_start).total_seconds() == 900


def test_interpolation_flags_for_hourly_source():
    d = date(2026, 6, 1)
    blocks = normalize_to_blocks(_synthetic_hourly_raw(d), d)
    # On-the-hour blocks (minute == 0) align to a native hourly sample -> not interpolated.
    on_hour = [b for b in blocks if b.block_start.minute == 0]
    off_hour = [b for b in blocks if b.block_start.minute != 0]
    assert all(not b.interpolated for b in on_hour)
    assert all(b.interpolated for b in off_hour)


def test_night_is_zero_and_noon_positive():
    d = date(2026, 6, 1)
    blocks = normalize_to_blocks(_synthetic_hourly_raw(d), d)
    assert blocks[0].poa == 0.0 and blocks[0].is_day == 0
    noon = blocks[48]  # ~12:00
    assert noon.poa > 0.0 and noon.is_day == 1


def test_radiation_never_negative():
    d = date(2026, 6, 1)
    blocks = normalize_to_blocks(_synthetic_hourly_raw(d), d)
    assert all((b.ghi or 0) >= 0 and (b.poa or 0) >= 0 for b in blocks)
