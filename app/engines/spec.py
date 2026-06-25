"""Runtime plant specification used by the engines (decoupled from the ORM)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PlantSpec:
    plant_code: str
    plant_name: str
    latitude: float
    longitude: float
    timezone: str
    config_version: int

    # Solar
    solar_ac_mw: float
    solar_dc_mw: float
    dc_ac_ratio: float
    solar_performance_ratio: float
    solar_loss_factor: float
    temp_coeff_pct_per_c: float
    panel_tilt: float
    panel_azimuth: float
    use_global_tilted_irradiance: bool

    # Wind
    wind_ac_mw: float
    wind_loss_factor: float
    wind_power_curve: list[list[float]]  # [[speed_ms, power_kw], ...]
    curve_rated_kw: float
    hub_height_m: float
    cut_in_ms: float
    rated_ms: float
    cut_out_ms: float
    air_density_correction: bool

    block_minutes: int = 15

    @classmethod
    def from_orm(cls, cfg) -> "PlantSpec":
        return cls(
            plant_code=cfg.plant_code,
            plant_name=cfg.plant_name,
            latitude=cfg.latitude,
            longitude=cfg.longitude,
            timezone=cfg.timezone,
            config_version=cfg.config_version,
            solar_ac_mw=cfg.solar_ac_mw,
            solar_dc_mw=cfg.solar_dc_mw,
            dc_ac_ratio=cfg.dc_ac_ratio,
            solar_performance_ratio=cfg.solar_performance_ratio,
            solar_loss_factor=cfg.solar_loss_factor,
            temp_coeff_pct_per_c=cfg.temp_coeff_pct_per_c,
            panel_tilt=cfg.panel_tilt,
            panel_azimuth=cfg.panel_azimuth,
            use_global_tilted_irradiance=cfg.use_global_tilted_irradiance,
            wind_ac_mw=cfg.wind_ac_mw,
            wind_loss_factor=cfg.wind_loss_factor,
            wind_power_curve=cfg.wind_power_curve,
            curve_rated_kw=cfg.curve_rated_kw,
            hub_height_m=cfg.hub_height_m,
            cut_in_ms=cfg.cut_in_ms,
            rated_ms=cfg.rated_ms,
            cut_out_ms=cfg.cut_out_ms,
            air_density_correction=cfg.air_density_correction,
            block_minutes=cfg.block_minutes,
        )
