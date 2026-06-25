"""Solar generation engine — per 15-minute block.

Pipeline (per spec):
  POA -> cell-temperature derate -> DC -> inverter clipping -> night zeroing.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.engines.spec import PlantSpec
from app.weather.normalize import NormalizedBlock

NOCT_C = 45.0  # Nominal Operating Cell Temperature
NOCT_REF_IRRADIANCE = 800.0


@dataclass
class SolarResult:
    ac_mw: float
    mwh: float
    cuf: float
    status: str  # OK / CLIPPED / NIGHT / INTERPOLATED
    poa: float
    t_cell: float


def simulate_solar_block(spec: PlantSpec, block: NormalizedBlock) -> SolarResult:
    block_hours = spec.block_minutes / 60.0
    poa = float(block.poa or 0.0)
    t_amb = block.temperature_2m if block.temperature_2m is not None else 25.0

    # Night: no daylight or no irradiance.
    if (block.is_day == 0) or poa <= 0.0:
        status = "NIGHT"
        if block.interpolated:
            status = "NIGHT"  # night dominates; interpolation tracked separately
        return SolarResult(0.0, 0.0, 0.0, status, poa, t_amb)

    # Cell temperature derate.
    t_cell = t_amb + (poa / NOCT_REF_IRRADIANCE) * (NOCT_C - 20.0)
    temp_factor = 1.0 + (spec.temp_coeff_pct_per_c / 100.0) * (t_cell - 25.0)

    dc_mw = (
        spec.solar_dc_mw
        * (poa / 1000.0)
        * spec.solar_performance_ratio
        * temp_factor
        * (1.0 - spec.solar_loss_factor)
    )
    dc_mw = max(0.0, dc_mw)

    # Inverter clipping at AC capacity.
    ac_mw = min(dc_mw, spec.solar_ac_mw)
    ac_mw = max(0.0, ac_mw)

    if dc_mw > spec.solar_ac_mw + 1e-9:
        status = "CLIPPED"
    elif block.interpolated:
        status = "INTERPOLATED"
    else:
        status = "OK"

    mwh = ac_mw * block_hours
    rated_mwh = spec.solar_ac_mw * block_hours
    cuf = (mwh / rated_mwh) if rated_mwh > 0 else 0.0
    return SolarResult(ac_mw, mwh, cuf, status, poa, t_cell)
