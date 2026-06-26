"""Read-only plant endpoints (1-6). All require a valid API key."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import AuthContext, require_admin, require_read
from app.api.repository import (
    get_blocks,
    get_blocks_range,
    get_summaries_range,
    get_summary,
    get_weather_blocks,
)
from app.api.schemas import (
    BlockOut,
    BlockSeriesOut,
    DailySummaryOut,
    PlantConfigOut,
    PlantConfigUpdate,
    SummaryListOut,
    WeatherBlockOut,
    WeatherSeriesOut,
)
from app.db.base import session_scope
from app.db.models import DailySummary, GenerationBlock, PlantConfig, SimulationVersion
from app.services import create_config_version
from app.simulate import load_active_config

router = APIRouter(prefix="/plants", tags=["plants"])


def _block_to_out(b: GenerationBlock) -> BlockOut:
    return BlockOut(
        block_no=b.block_no,
        block_start=b.block_start,
        block_end=b.block_end,
        solar_mw=round(b.solar_mw, 4),
        solar_mwh=round(b.solar_mwh, 5),
        wind_mw=round(b.wind_mw, 4),
        wind_mwh=round(b.wind_mwh, 5),
        total_mw=round(b.total_mw, 4),
        total_mwh=round(b.total_mwh, 5),
        solar_cuf=round(b.solar_cuf, 5),
        wind_cuf=round(b.wind_cuf, 5),
        hybrid_cuf=round(b.hybrid_cuf, 5),
        solar_status=b.solar_status,
        wind_status=b.wind_status,
        data_mode=b.data_mode,
        data_label=b.data_label,
        data_quality_status=b.data_quality_status,
    )


def _summary_to_out(s: DailySummary) -> DailySummaryOut:
    return DailySummaryOut(
        plant_code=s.plant_code,
        sim_date=s.sim_date,
        data_mode=s.data_mode,
        data_label=s.data_label,
        data_quality_status=s.data_quality_status,
        solar_mwh=round(s.solar_mwh, 4),
        wind_mwh=round(s.wind_mwh, 4),
        total_mwh=round(s.total_mwh, 4),
        solar_peak_mw=round(s.solar_peak_mw, 4),
        wind_peak_mw=round(s.wind_peak_mw, 4),
        total_peak_mw=round(s.total_peak_mw, 4),
        solar_cuf=round(s.solar_cuf, 5),
        wind_cuf=round(s.wind_cuf, 5),
        hybrid_cuf=round(s.hybrid_cuf, 5),
        solar_specific_yield=round(s.solar_specific_yield, 4),
        blocks_count=s.blocks_count,
        simulation_version=s.simulation_version,
    )


@router.get("/{code}/config", response_model=PlantConfigOut)
def plant_config(code: str, ctx: AuthContext = Depends(require_read)):
    """Endpoint 1: capacities, location, timezone, active assumptions."""
    with session_scope() as db:
        try:
            cfg = load_active_config(db, code)
        except ValueError:
            raise HTTPException(404, f"Unknown plant '{code}'") from None
        ver = db.query(SimulationVersion).order_by(SimulationVersion.created_at.desc()).first()
        return PlantConfigOut(
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
            hub_height_m=cfg.hub_height_m,
            cut_in_ms=cfg.cut_in_ms,
            rated_ms=cfg.rated_ms,
            cut_out_ms=cfg.cut_out_ms,
            air_density_correction=cfg.air_density_correction,
            block_minutes=cfg.block_minutes,
            wind_power_curve=cfg.wind_power_curve,
            assumptions=(ver.assumptions if ver else {}),
        )


def _config_to_out(cfg: PlantConfig, assumptions: dict) -> PlantConfigOut:
    return PlantConfigOut(
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
        hub_height_m=cfg.hub_height_m,
        cut_in_ms=cfg.cut_in_ms,
        rated_ms=cfg.rated_ms,
        cut_out_ms=cfg.cut_out_ms,
        air_density_correction=cfg.air_density_correction,
        block_minutes=cfg.block_minutes,
        wind_power_curve=cfg.wind_power_curve,
        assumptions=assumptions,
    )


@router.put("/{code}/config", response_model=PlantConfigOut)
def update_plant_config(
    code: str, body: PlantConfigUpdate, ctx: AuthContext = Depends(require_admin)
):
    """Update plant config (capacities, lat/long, timezone, ...) — ADMIN.

    Creates a NEW versioned config (history preserved); the new version becomes active.
    Re-run a simulation afterwards to regenerate outputs under the new assumptions.
    """
    fields = body.model_dump(exclude_unset=True, exclude_none=True)
    with session_scope() as db:
        try:
            new_cfg = create_config_version(db, code, fields)
        except ValueError:
            raise HTTPException(404, f"Unknown plant '{code}'") from None
        ver = db.query(SimulationVersion).order_by(SimulationVersion.created_at.desc()).first()
        return _config_to_out(new_cfg, ver.assumptions if ver else {})


def _weather_block_to_out(w) -> WeatherBlockOut:
    return WeatherBlockOut(
        block_no=w.block_no,
        block_start=w.block_start,
        block_end=w.block_end,
        interpolated=w.interpolated,
        ghi=w.ghi,
        poa=w.poa,
        dni=w.dni,
        dhi=w.dhi,
        temperature_2m=w.temperature_2m,
        cloud_cover=w.cloud_cover,
        is_day=w.is_day,
        wind_speed_10m=w.wind_speed_10m,
        wind_speed_100m=w.wind_speed_100m,
        wind_speed_120m=w.wind_speed_120m,
        wind_direction_100m=w.wind_direction_100m,
        wind_gusts_10m=w.wind_gusts_10m,
        surface_pressure=w.surface_pressure,
    )


@router.get("/{code}/weather", response_model=WeatherSeriesOut)
def weather(
    code: str,
    sim_date: date = Query(..., alias="date"),
    mode: str | None = Query(None, description="HISTORICAL | LIVE | FORECAST"),
    ctx: AuthContext = Depends(require_read),
):
    """Normalized 15-minute weather variables used for a given date."""
    with session_scope() as db:
        blocks = get_weather_blocks(db, code, sim_date, mode.upper() if mode else None)
        if not blocks:
            raise HTTPException(404, f"No weather data for {code} on {sim_date}.")
        return WeatherSeriesOut(
            plant_code=code,
            sim_date=sim_date,
            data_mode=blocks[0].data_mode,
            weather_source=blocks[0].weather_source,
            block_count=len(blocks),
            blocks=[_weather_block_to_out(b) for b in blocks],
        )


def _series(code: str, sim_date: date, data_mode: str, current_block: int | None = None):
    with session_scope() as db:
        blocks = get_blocks(db, code, sim_date, data_mode)
        if not blocks:
            raise HTTPException(
                404,
                f"No {data_mode} simulation for {code} on {sim_date}. "
                f"Trigger a run via POST /admin/reprocess.",
            )
        return BlockSeriesOut(
            plant_code=code,
            sim_date=sim_date,
            data_label=blocks[0].data_label,
            data_mode=data_mode,
            simulation_version=blocks[0].simulation_version,
            weather_source=blocks[0].weather_source,
            block_count=len(blocks),
            current_block_no=current_block,
            blocks=[_block_to_out(b) for b in blocks],
        )


@router.get("/{code}/historical", response_model=BlockSeriesOut)
def historical(
    code: str,
    sim_date: date = Query(..., alias="date", description="Completed date (YYYY-MM-DD)"),
    ctx: AuthContext = Depends(require_read),
):
    """Endpoint 2: block-wise simulated generation for a completed date."""
    return _series(code, sim_date, "HISTORICAL")


@router.get("/{code}/live", response_model=BlockSeriesOut)
def live(code: str, ctx: AuthContext = Depends(require_read)):
    """Endpoint 3: today's blocks (completed = LIVE_ESTIMATED, remaining = FORECAST)."""
    with session_scope() as db:
        cfg = load_active_config(db, code)
        tz = cfg.timezone
    now = datetime.now(ZoneInfo(tz))
    today = now.date()
    current_block = now.hour * 4 + now.minute // 15 + 1
    return _series(code, today, "LIVE", current_block)


@router.get("/{code}/forecast", response_model=BlockSeriesOut)
def forecast(
    code: str,
    sim_date: date | None = Query(None, alias="date", description="Future date (YYYY-MM-DD)"),
    horizon_days: int | None = Query(
        None, ge=0, le=16, description="Days ahead (0=rest of today, 1, 3, 7...)"
    ),
    ctx: AuthContext = Depends(require_read),
):
    """Endpoint 4: future expected generation (by explicit date or horizon)."""
    with session_scope() as db:
        cfg = load_active_config(db, code)
        tz = cfg.timezone
    today = datetime.now(ZoneInfo(tz)).date()
    if sim_date is None:
        sim_date = today + timedelta(days=horizon_days if horizon_days is not None else 1)
    return _series(code, sim_date, "FORECAST")


@router.get("/{code}/summary", response_model=SummaryListOut)
def summary(
    code: str,
    sim_date: date | None = Query(None, alias="date"),
    start: date | None = Query(None),
    end: date | None = Query(None),
    ctx: AuthContext = Depends(require_read),
):
    """Endpoint 5: daily solar/wind/hybrid summary (single date or date range)."""
    with session_scope() as db:
        if start and end:
            rows = get_summaries_range(db, code, start, end)
        else:
            target = sim_date or datetime.now(ZoneInfo("UTC")).date()
            row = get_summary(db, code, target)
            rows = [row] if row else []
        if not rows:
            raise HTTPException(404, f"No summary found for {code}.")
        return SummaryListOut(
            plant_code=code,
            count=len(rows),
            summaries=[_summary_to_out(s) for s in rows],
        )


@router.get("/{code}/range", response_model=list[BlockSeriesOut])
def block_range(
    code: str,
    start: date = Query(...),
    end: date = Query(...),
    ctx: AuthContext = Depends(require_read),
):
    """Endpoint 6: block-wise generation over a date range (grouped per day)."""
    if (end - start).days > 31:
        raise HTTPException(400, "Range too large; max 31 days.")
    with session_scope() as db:
        blocks = get_blocks_range(db, code, start, end)
        if not blocks:
            raise HTTPException(404, f"No simulation data for {code} in range.")
        by_day: dict[date, list[GenerationBlock]] = {}
        for b in blocks:
            by_day.setdefault(b.sim_date, []).append(b)
        out = []
        for d in sorted(by_day):
            day_blocks = by_day[d]
            out.append(
                BlockSeriesOut(
                    plant_code=code,
                    sim_date=d,
                    data_label=day_blocks[0].data_label,
                    data_mode=day_blocks[0].data_mode,
                    simulation_version=day_blocks[0].simulation_version,
                    weather_source=day_blocks[0].weather_source,
                    block_count=len(day_blocks),
                    blocks=[_block_to_out(b) for b in day_blocks],
                )
            )
        return out
