"""Deterministic, reproducible micro-variability for realistic plant 'feel'.

NWP-derived 15-min weather is smooth; real metered generation is jagged (passing
clouds, wind turbulence). This adds bounded, weather-correlated texture seeded by the
block timestamp, so the same date always produces the same output (reproducible) while
caps / night-zero / no-negative invariants still hold.
"""
from __future__ import annotations

import hashlib
from datetime import datetime


def block_noise(block_start: datetime, channel: str) -> float:
    """Deterministic pseudo-random value in [-1, 1] for a block + channel."""
    seed = f"{block_start.isoformat()}|{channel}"
    digest = hashlib.md5(seed.encode("utf-8")).digest()
    val = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF  # [0, 1]
    return val * 2.0 - 1.0


def solar_texture_factor(block_start: datetime, cloud_cover: float | None) -> float:
    """Cloud-driven solar flicker. Clear sky -> ~1.0 (smooth); heavy cloud -> volatile."""
    cloud = 0.0 if cloud_cover is None else max(0.0, min(100.0, cloud_cover))
    # Amplitude grows with cloud cover (clear days stay clean), capped at 18%.
    amp = 0.18 * (cloud / 100.0)
    return max(0.0, 1.0 + amp * block_noise(block_start, "solar"))


def wind_texture_factor(
    block_start: datetime,
    wind_speed: float | None,
    wind_gust: float | None,
) -> float:
    """Turbulence-driven wind variability from the gust factor (always some jitter)."""
    ti = 0.08  # baseline turbulence intensity
    if wind_gust and wind_speed and wind_speed > 0:
        ti = min(0.5, max(0.05, (wind_gust - wind_speed) / wind_speed))
    amp = 0.6 * ti  # scale TI to a multiplicative amplitude
    return max(0.0, 1.0 + amp * block_noise(block_start, "wind"))
