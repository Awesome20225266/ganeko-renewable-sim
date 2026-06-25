"""Hybrid engine — combines solar + wind into one per-block record."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.engines.solar import simulate_solar_block
from app.engines.spec import PlantSpec
from app.engines.wind import simulate_wind_block
from app.weather.normalize import NormalizedBlock


@dataclass
class BlockResult:
    block_no: int
    block_start: datetime
    block_end: datetime
    solar_mw: float
    solar_mwh: float
    wind_mw: float
    wind_mwh: float
    total_mw: float
    total_mwh: float
    solar_cuf: float
    wind_cuf: float
    hybrid_cuf: float
    solar_status: str
    wind_status: str
    interpolated: bool
    data_quality_status: str


def simulate_block(spec: PlantSpec, block: NormalizedBlock) -> BlockResult:
    solar = simulate_solar_block(spec, block)
    wind = simulate_wind_block(spec, block)

    total_mw = solar.ac_mw + wind.mw
    total_mwh = solar.mwh + wind.mwh
    block_hours = spec.block_minutes / 60.0
    hybrid_rated_mwh = (spec.solar_ac_mw + spec.wind_ac_mw) * block_hours
    hybrid_cuf = (total_mwh / hybrid_rated_mwh) if hybrid_rated_mwh > 0 else 0.0

    dq = "INTERPOLATED" if block.interpolated else "OK"

    return BlockResult(
        block_no=block.block_no,
        block_start=block.block_start,
        block_end=block.block_end,
        solar_mw=solar.ac_mw,
        solar_mwh=solar.mwh,
        wind_mw=wind.mw,
        wind_mwh=wind.mwh,
        total_mw=total_mw,
        total_mwh=total_mwh,
        solar_cuf=solar.cuf,
        wind_cuf=wind.cuf,
        hybrid_cuf=hybrid_cuf,
        solar_status=solar.status,
        wind_status=wind.status,
        interpolated=block.interpolated,
        data_quality_status=dq,
    )


def simulate_day(spec: PlantSpec, blocks: list[NormalizedBlock]) -> list[BlockResult]:
    return [simulate_block(spec, b) for b in blocks]
