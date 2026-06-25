"""Pydantic request/response schemas. Never expose internal IDs or paths."""
from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class PlantConfigOut(BaseModel):
    plant_code: str
    plant_name: str
    latitude: float
    longitude: float
    timezone: str
    config_version: int
    solar_ac_mw: float
    solar_dc_mw: float
    dc_ac_ratio: float
    solar_performance_ratio: float
    solar_loss_factor: float
    temp_coeff_pct_per_c: float
    panel_tilt: float
    panel_azimuth: float
    use_global_tilted_irradiance: bool
    wind_ac_mw: float
    wind_loss_factor: float
    hub_height_m: float
    cut_in_ms: float
    rated_ms: float
    cut_out_ms: float
    air_density_correction: bool
    block_minutes: int
    wind_power_curve: list[list[float]]
    assumptions: dict


class PlantConfigUpdate(BaseModel):
    """Editable plant fields. Any provided field overrides the current active config;
    omitted fields are inherited. Saving creates a new config_version (history kept)."""

    plant_name: str | None = None
    latitude: float | None = Field(None, ge=-90, le=90)
    longitude: float | None = Field(None, ge=-180, le=180)
    timezone: str | None = None
    solar_ac_mw: float | None = Field(None, ge=0)
    solar_dc_mw: float | None = Field(None, ge=0)
    solar_performance_ratio: float | None = Field(None, gt=0, le=1)
    solar_loss_factor: float | None = Field(None, ge=0, lt=1)
    temp_coeff_pct_per_c: float | None = None
    panel_tilt: float | None = Field(None, ge=0, le=90)
    panel_azimuth: float | None = Field(None, ge=0, le=360)
    use_global_tilted_irradiance: bool | None = None
    wind_ac_mw: float | None = Field(None, ge=0)
    wind_loss_factor: float | None = Field(None, ge=0, lt=1)
    hub_height_m: float | None = Field(None, gt=0)
    cut_in_ms: float | None = Field(None, ge=0)
    rated_ms: float | None = Field(None, gt=0)
    cut_out_ms: float | None = Field(None, gt=0)
    air_density_correction: bool | None = None
    wind_power_curve: list[list[float]] | None = None


class WeatherBlockOut(BaseModel):
    block_no: int
    block_start: datetime
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
    wind_direction_100m: float | None = None
    wind_gusts_10m: float | None = None
    surface_pressure: float | None = None


class WeatherSeriesOut(BaseModel):
    plant_code: str
    sim_date: date
    data_mode: str
    weather_source: str | None = None
    block_count: int
    blocks: list[WeatherBlockOut]


class BlockOut(BaseModel):
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
    data_mode: str
    data_label: str
    data_quality_status: str


class BlockSeriesOut(BaseModel):
    plant_code: str
    sim_date: date
    data_label: str
    data_mode: str
    simulation_version: str
    weather_source: str | None = None
    block_count: int
    current_block_no: int | None = None
    blocks: list[BlockOut]


class DailySummaryOut(BaseModel):
    plant_code: str
    sim_date: date
    data_mode: str
    data_label: str
    data_quality_status: str
    solar_mwh: float
    wind_mwh: float
    total_mwh: float
    solar_peak_mw: float
    wind_peak_mw: float
    total_peak_mw: float
    solar_cuf: float
    wind_cuf: float
    hybrid_cuf: float
    solar_specific_yield: float
    blocks_count: int
    simulation_version: str


class SummaryListOut(BaseModel):
    plant_code: str
    count: int
    summaries: list[DailySummaryOut]


# ---- Admin ----------------------------------------------------------------
class ReprocessRequest(BaseModel):
    plant_code: str
    dates: list[date] = Field(..., min_length=1)
    mode: str | None = Field(
        None, description="HISTORICAL | LIVE | FORECAST; auto-detected if omitted"
    )


class ReprocessResultItem(BaseModel):
    sim_date: date
    mode: str
    status: str
    data_label: str
    blocks_written: int
    quality_status: str
    total_mwh: float
    issues: list[str] = []


class ReprocessResponse(BaseModel):
    plant_code: str
    triggered: int
    results: list[ReprocessResultItem]


class CreateKeyRequest(BaseModel):
    team: str
    name: str
    scope: str = Field("read", pattern="^(read|admin)$")
    rate_limit_per_min: int = 120
    expires_in_days: int | None = None


class CreateKeyResponse(BaseModel):
    api_key: str = Field(..., description="Shown ONCE — store securely.")
    key_prefix: str
    team: str
    name: str
    scope: str
    rate_limit_per_min: int
    expires_at: datetime | None


class KeyInfoOut(BaseModel):
    key_prefix: str
    team: str
    name: str
    scope: str
    is_active: bool
    rate_limit_per_min: int
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None


class KeyListOut(BaseModel):
    count: int
    keys: list[KeyInfoOut]


class MessageOut(BaseModel):
    message: str
