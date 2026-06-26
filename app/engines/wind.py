"""Wind generation engine — per 15-minute block.

Pipeline (per spec):
  hub-height wind speed (nearest level, else power-law extrapolation)
  -> optional air-density correction
  -> power-curve interpolation (cut-in / rated / cut-out enforced)
  -> losses + AC cap.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from app.engines.spec import PlantSpec
from app.engines.texture import wind_texture_factor
from app.weather.normalize import NormalizedBlock

WIND_SHEAR_ALPHA = 0.143  # power-law exponent (~1/7), open terrain
RHO0 = 1.225  # kg/m^3 reference air density
R_SPECIFIC_AIR = 287.05  # J/(kg*K)


@dataclass
class WindResult:
    mw: float
    mwh: float
    cuf: float
    status: str  # OK / CLIPPED / CALM / CUTOUT / INTERPOLATED / MISSING
    v_hub: float


def _hub_speed(spec: PlantSpec, block: NormalizedBlock) -> float | None:
    levels: dict[float, float] = {}
    if block.wind_speed_10m is not None:
        levels[10.0] = block.wind_speed_10m
    if block.extra.get("wind_speed_80m") is not None:
        levels[80.0] = block.extra["wind_speed_80m"]
    if block.wind_speed_100m is not None:
        levels[100.0] = block.wind_speed_100m
    if block.wind_speed_120m is not None:
        levels[120.0] = block.wind_speed_120m
    if block.wind_speed_180m is not None:
        levels[180.0] = block.wind_speed_180m
    if not levels:
        return None

    nearest = min(levels, key=lambda h: abs(h - spec.hub_height_m))
    v_ref = max(0.0, levels[nearest])
    if abs(nearest - spec.hub_height_m) < 1e-6 or v_ref == 0.0:
        return v_ref
    return v_ref * (spec.hub_height_m / nearest) ** WIND_SHEAR_ALPHA


def _curve_power_mw(spec: PlantSpec, v: float) -> float:
    curve = sorted(spec.wind_power_curve, key=lambda p: p[0])
    speeds = np.array([p[0] for p in curve], dtype=float)
    powers_kw = np.array([p[1] for p in curve], dtype=float)
    p_kw = float(np.interp(v, speeds, powers_kw))
    return max(0.0, p_kw) / 1000.0


def _air_density_factor(block: NormalizedBlock) -> float:
    if block.surface_pressure is None or block.temperature_2m is None:
        return 1.0
    p_pa = block.surface_pressure * 100.0  # hPa -> Pa
    t_k = block.temperature_2m + 273.15
    if t_k <= 0:
        return 1.0
    rho = p_pa / (R_SPECIFIC_AIR * t_k)
    return rho / RHO0


def simulate_wind_block(
    spec: PlantSpec, block: NormalizedBlock, texture: bool = False
) -> WindResult:
    block_hours = spec.block_minutes / 60.0
    v_hub = _hub_speed(spec, block)
    if v_hub is None:
        return WindResult(0.0, 0.0, 0.0, "MISSING", 0.0)

    # Hard cut-in / cut-out bounds.
    if v_hub < spec.cut_in_ms:
        status = "CALM"
        return WindResult(0.0, 0.0, 0.0, status, v_hub)
    if v_hub > spec.cut_out_ms:
        return WindResult(0.0, 0.0, 0.0, "CUTOUT", v_hub)

    power_mw = _curve_power_mw(spec, v_hub)

    if spec.air_density_correction:
        power_mw *= _air_density_factor(block)

    power_mw *= 1.0 - spec.wind_loss_factor
    # Turbulence-driven variability (gust factor); capped at AC below.
    if texture:
        power_mw *= wind_texture_factor(
            block.block_start, block.wind_speed_10m, block.wind_gusts_10m
        )
    power_mw = max(0.0, power_mw)

    if power_mw > spec.wind_ac_mw + 1e-9:
        power_mw = spec.wind_ac_mw
        status = "CLIPPED"
    elif block.interpolated:
        status = "INTERPOLATED"
    else:
        status = "OK"

    mwh = power_mw * block_hours
    rated_mwh = spec.wind_ac_mw * block_hours
    cuf = (mwh / rated_mwh) if rated_mwh > 0 else 0.0
    return WindResult(power_mw, mwh, cuf, status, v_hub)
