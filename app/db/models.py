"""Database models.

Separate concerns per the spec: plant config (versioned), weather-provider config,
raw weather responses, normalized 15-min weather, generation output (the per-block
hybrid record holding solar+wind+total), daily summaries, API keys, API usage logs,
simulation run logs, error logs, and a simulation-version registry.

Design note: the spec lists "solar output / wind output / hybrid output" as separate
stores. The hybrid engine emits ONE per-block record containing solar_mw, wind_mw and
total together, so we keep a single `generation_block` table (independently queryable
by `data_mode` = HISTORICAL/LIVE/FORECAST). History is preserved via versioning: a
reprocess marks prior rows `is_current=False` and inserts new versioned rows instead
of overwriting. This avoids 3x write amplification with no query benefit; documented
in README.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Plant identity + versioned configuration
# --------------------------------------------------------------------------- #
class Plant(Base):
    __tablename__ = "plant"

    id: Mapped[int] = mapped_column(primary_key=True)
    plant_code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    plant_name: Mapped[str] = mapped_column(String(255))
    active_config_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    configs: Mapped[list[PlantConfig]] = relationship(
        back_populates="plant", cascade="all, delete-orphan"
    )


class PlantConfig(Base):
    __tablename__ = "plant_config"
    __table_args__ = (
        UniqueConstraint("plant_code", "config_version", name="uq_plant_config_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plant_id: Mapped[int] = mapped_column(ForeignKey("plant.id"))
    plant_code: Mapped[str] = mapped_column(String(64), index=True)
    config_version: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Location
    plant_name: Mapped[str] = mapped_column(String(255))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    timezone: Mapped[str] = mapped_column(String(64))

    # Solar
    solar_ac_mw: Mapped[float] = mapped_column(Float)
    solar_dc_mw: Mapped[float] = mapped_column(Float)
    dc_ac_ratio: Mapped[float] = mapped_column(Float)
    solar_performance_ratio: Mapped[float] = mapped_column(Float, default=0.80)
    solar_loss_factor: Mapped[float] = mapped_column(Float, default=0.0)
    temp_coeff_pct_per_c: Mapped[float] = mapped_column(Float, default=-0.40)
    panel_tilt: Mapped[float] = mapped_column(Float, default=0.0)
    panel_azimuth: Mapped[float] = mapped_column(Float, default=180.0)
    use_global_tilted_irradiance: Mapped[bool] = mapped_column(Boolean, default=False)

    # Wind
    wind_ac_mw: Mapped[float] = mapped_column(Float)
    wind_loss_factor: Mapped[float] = mapped_column(Float, default=0.0)
    # Power curve: list of [wind_speed_ms, power_kw] points (per single reference turbine
    # OR for the whole farm, scaled to wind_ac_mw via curve_rated_kw).
    wind_power_curve: Mapped[list] = mapped_column(JSON)
    curve_rated_kw: Mapped[float] = mapped_column(Float, default=0.0)
    hub_height_m: Mapped[float] = mapped_column(Float, default=100.0)
    cut_in_ms: Mapped[float] = mapped_column(Float, default=3.0)
    rated_ms: Mapped[float] = mapped_column(Float, default=12.0)
    cut_out_ms: Mapped[float] = mapped_column(Float, default=25.0)
    air_density_correction: Mapped[bool] = mapped_column(Boolean, default=False)

    # General
    block_minutes: Mapped[int] = mapped_column(Integer, default=15)
    api_access_rules: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    plant: Mapped[Plant] = relationship(back_populates="configs")


# --------------------------------------------------------------------------- #
# Weather provider configuration
# --------------------------------------------------------------------------- #
class WeatherProviderConfig(Base):
    __tablename__ = "weather_provider_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    forecast_url: Mapped[str] = mapped_column(String(512))
    historical_forecast_url: Mapped[str] = mapped_column(String(512))
    archive_url: Mapped[str] = mapped_column(String(512))
    solar_variables: Mapped[list] = mapped_column(JSON)
    wind_variables: Mapped[list] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# --------------------------------------------------------------------------- #
# Raw weather responses (verbatim, kept as history)
# --------------------------------------------------------------------------- #
class RawWeatherResponse(Base):
    __tablename__ = "raw_weather_response"
    __table_args__ = (
        Index("ix_raw_plant_date_mode", "plant_code", "sim_date", "data_mode"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plant_code: Mapped[str] = mapped_column(String(64), index=True)
    sim_date: Mapped[date] = mapped_column(Date)
    data_mode: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(64))
    request_url: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    raw_json: Mapped[dict] = mapped_column(JSON)


# --------------------------------------------------------------------------- #
# Normalized 15-minute weather (96 blocks/day)
# --------------------------------------------------------------------------- #
class WeatherBlock(Base):
    __tablename__ = "weather_block"
    __table_args__ = (
        UniqueConstraint(
            "plant_code", "sim_date", "block_no", "data_mode", name="uq_weather_block"
        ),
        Index("ix_weather_plant_date_mode", "plant_code", "sim_date", "data_mode"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plant_code: Mapped[str] = mapped_column(String(64), index=True)
    sim_date: Mapped[date] = mapped_column(Date)
    block_no: Mapped[int] = mapped_column(Integer)  # 1..96
    block_start: Mapped[datetime] = mapped_column(DateTime)  # local naive
    block_end: Mapped[datetime] = mapped_column(DateTime)

    data_mode: Mapped[str] = mapped_column(String(32))
    weather_source: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    interpolated: Mapped[bool] = mapped_column(Boolean, default=False)

    # Solar weather
    ghi: Mapped[float | None] = mapped_column(Float, nullable=True)
    poa: Mapped[float | None] = mapped_column(Float, nullable=True)
    dni: Mapped[float | None] = mapped_column(Float, nullable=True)
    dhi: Mapped[float | None] = mapped_column(Float, nullable=True)
    temperature_2m: Mapped[float | None] = mapped_column(Float, nullable=True)
    cloud_cover: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_day: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Wind weather
    wind_speed_10m: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_speed_100m: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_speed_120m: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_speed_180m: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_direction_100m: Mapped[float | None] = mapped_column(Float, nullable=True)
    wind_gusts_10m: Mapped[float | None] = mapped_column(Float, nullable=True)
    surface_pressure: Mapped[float | None] = mapped_column(Float, nullable=True)


# --------------------------------------------------------------------------- #
# Generation output — the per-block hybrid record (solar + wind + total)
# --------------------------------------------------------------------------- #
class GenerationBlock(Base):
    __tablename__ = "generation_block"
    __table_args__ = (
        UniqueConstraint(
            "plant_code",
            "sim_date",
            "block_no",
            "data_mode",
            "simulation_version",
            "model_assumption_version",
            name="uq_generation_block",
        ),
        Index("ix_gen_plant_date_mode_cur", "plant_code", "sim_date", "data_mode", "is_current"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plant_code: Mapped[str] = mapped_column(String(64), index=True)
    sim_date: Mapped[date] = mapped_column(Date)
    block_no: Mapped[int] = mapped_column(Integer)
    block_start: Mapped[datetime] = mapped_column(DateTime)
    block_end: Mapped[datetime] = mapped_column(DateTime)

    solar_mw: Mapped[float] = mapped_column(Float, default=0.0)
    solar_mwh: Mapped[float] = mapped_column(Float, default=0.0)
    wind_mw: Mapped[float] = mapped_column(Float, default=0.0)
    wind_mwh: Mapped[float] = mapped_column(Float, default=0.0)
    total_mw: Mapped[float] = mapped_column(Float, default=0.0)
    total_mwh: Mapped[float] = mapped_column(Float, default=0.0)

    solar_cuf: Mapped[float] = mapped_column(Float, default=0.0)
    wind_cuf: Mapped[float] = mapped_column(Float, default=0.0)
    hybrid_cuf: Mapped[float] = mapped_column(Float, default=0.0)

    solar_status: Mapped[str] = mapped_column(String(24), default="OK")
    wind_status: Mapped[str] = mapped_column(String(24), default="OK")

    data_mode: Mapped[str] = mapped_column(String(32))  # HISTORICAL / LIVE / FORECAST
    data_source: Mapped[str] = mapped_column(String(64))
    data_label: Mapped[str] = mapped_column(String(32))  # HISTORICAL_SIMULATED, etc.
    data_quality_status: Mapped[str] = mapped_column(String(24), default="OK")

    # Versioning / lineage
    simulation_version: Mapped[str] = mapped_column(String(32))
    model_assumption_version: Mapped[str] = mapped_column(String(32))
    plant_config_version: Mapped[int] = mapped_column(Integer)
    weather_source: Mapped[str] = mapped_column(String(64))
    weather_fetch_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    weather_model_used: Mapped[str | None] = mapped_column(String(64), nullable=True)
    forecast_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# --------------------------------------------------------------------------- #
# Daily summaries
# --------------------------------------------------------------------------- #
class DailySummary(Base):
    __tablename__ = "daily_summary"
    __table_args__ = (
        UniqueConstraint(
            "plant_code",
            "sim_date",
            "data_mode",
            "simulation_version",
            "model_assumption_version",
            name="uq_daily_summary",
        ),
        Index("ix_summary_plant_date_cur", "plant_code", "sim_date", "is_current"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plant_code: Mapped[str] = mapped_column(String(64), index=True)
    sim_date: Mapped[date] = mapped_column(Date)

    solar_mwh: Mapped[float] = mapped_column(Float, default=0.0)
    wind_mwh: Mapped[float] = mapped_column(Float, default=0.0)
    total_mwh: Mapped[float] = mapped_column(Float, default=0.0)
    solar_peak_mw: Mapped[float] = mapped_column(Float, default=0.0)
    wind_peak_mw: Mapped[float] = mapped_column(Float, default=0.0)
    total_peak_mw: Mapped[float] = mapped_column(Float, default=0.0)
    solar_cuf: Mapped[float] = mapped_column(Float, default=0.0)
    wind_cuf: Mapped[float] = mapped_column(Float, default=0.0)
    hybrid_cuf: Mapped[float] = mapped_column(Float, default=0.0)
    solar_specific_yield: Mapped[float] = mapped_column(Float, default=0.0)  # kWh/kWp/day
    blocks_count: Mapped[int] = mapped_column(Integer, default=0)

    data_mode: Mapped[str] = mapped_column(String(32))
    data_label: Mapped[str] = mapped_column(String(32))
    data_quality_status: Mapped[str] = mapped_column(String(24), default="OK")

    simulation_version: Mapped[str] = mapped_column(String(32))
    model_assumption_version: Mapped[str] = mapped_column(String(32))
    plant_config_version: Mapped[int] = mapped_column(Integer)
    weather_source: Mapped[str] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# --------------------------------------------------------------------------- #
# API keys, usage logs
# --------------------------------------------------------------------------- #
class ApiKey(Base):
    __tablename__ = "api_key"

    id: Mapped[int] = mapped_column(primary_key=True)
    team: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(128))
    key_prefix: Mapped[str] = mapped_column(String(16), index=True)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True)  # sha256 hex
    scope: Mapped[str] = mapped_column(String(16), default="read")  # read | admin
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    rate_limit_per_min: Mapped[int] = mapped_column(Integer, default=120)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApiUsageLog(Base):
    __tablename__ = "api_usage_log"
    __table_args__ = (Index("ix_usage_key_ts", "api_key_id", "ts"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    api_key_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    key_prefix: Mapped[str | None] = mapped_column(String(16), nullable=True)
    path: Mapped[str] = mapped_column(String(255))
    method: Mapped[str] = mapped_column(String(8))
    status_code: Mapped[int] = mapped_column(Integer)
    client_host: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# --------------------------------------------------------------------------- #
# Run logs, error logs, version registry
# --------------------------------------------------------------------------- #
class SimulationRun(Base):
    __tablename__ = "simulation_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    plant_code: Mapped[str] = mapped_column(String(64), index=True)
    sim_date: Mapped[date] = mapped_column(Date)
    data_mode: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24))  # OK / PARTIAL / FAILED / REPROCESSED
    blocks_written: Mapped[int] = mapped_column(Integer, default=0)
    simulation_version: Mapped[str] = mapped_column(String(32))
    triggered_by: Mapped[str] = mapped_column(String(32), default="scheduler")
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ErrorLog(Base):
    __tablename__ = "error_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    context: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text)
    traceback: Mapped[str | None] = mapped_column(Text, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SimulationVersion(Base):
    __tablename__ = "simulation_version"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(32), unique=True)
    description: Mapped[str] = mapped_column(Text)
    model_assumption_version: Mapped[str] = mapped_column(String(32))
    assumptions: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
