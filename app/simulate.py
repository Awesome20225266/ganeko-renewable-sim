"""Simulation orchestrator: fetch -> raw store -> normalize -> engines -> persist.

Handles caching (avoid duplicate fetches unless reprocess), versioning (preserve
history by demoting prior `is_current` rows), per-block live/forecast labelling,
data-quality enforcement, and run/error logging.
"""
from __future__ import annotations

import asyncio
import traceback
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.settings import Settings, get_settings
from app.db.base import session_scope
from app.db.models import (
    DailySummary,
    ErrorLog,
    GenerationBlock,
    PlantConfig,
    SimulationRun,
)
from app.engines.hybrid import BlockResult, simulate_day
from app.engines.spec import PlantSpec
from app.logging_conf import get_logger
from app.quality import QualityReport, check_day
from app.weather.client import DataMode, PlantLike, fetch_weather, resolve_mode
from app.weather.normalize import normalize_to_blocks
from app.weather.store import find_cached_raw, persist_raw, persist_weather_blocks

logger = get_logger(__name__)

MODE_LABEL = {
    DataMode.HISTORICAL: "HISTORICAL_SIMULATED",
    DataMode.LIVE: "LIVE_ESTIMATED",
    DataMode.FORECAST: "FORECAST_SIMULATED",
}


@dataclass
class RunSummary:
    plant_code: str
    sim_date: date
    mode: str
    status: str
    data_label: str
    blocks_written: int
    quality_status: str
    issues: list[str]
    solar_mwh: float
    wind_mwh: float
    total_mwh: float
    solar_cuf: float
    wind_cuf: float
    hybrid_cuf: float
    weather_source: str
    fetched_fresh: bool


def load_active_config(db: Session, plant_code: str) -> PlantConfig:
    cfg = db.scalar(
        select(PlantConfig)
        .where(PlantConfig.plant_code == plant_code, PlantConfig.is_active.is_(True))
        .order_by(PlantConfig.config_version.desc())
    )
    if cfg is None:
        raise ValueError(f"No active config for plant '{plant_code}'")
    return cfg


def _plantlike(spec: PlantSpec) -> PlantLike:
    return PlantLike(
        plant_code=spec.plant_code,
        latitude=spec.latitude,
        longitude=spec.longitude,
        timezone=spec.timezone,
        panel_tilt=spec.panel_tilt,
        panel_azimuth=spec.panel_azimuth,
        use_global_tilted_irradiance=spec.use_global_tilted_irradiance,
    )


def _current_block_no(tz: str, sim_date: date) -> int:
    now = datetime.now(ZoneInfo(tz))
    if now.date() != sim_date:
        return 96 if now.date() > sim_date else 0
    return now.hour * 4 + now.minute // 15 + 1


def _summarize(spec: PlantSpec, results: list[BlockResult]) -> dict:
    solar_mwh = sum(r.solar_mwh for r in results)
    wind_mwh = sum(r.wind_mwh for r in results)
    total_mwh = sum(r.total_mwh for r in results)
    hours = 24.0
    solar_cuf = solar_mwh / (spec.solar_ac_mw * hours) if spec.solar_ac_mw else 0.0
    wind_cuf = wind_mwh / (spec.wind_ac_mw * hours) if spec.wind_ac_mw else 0.0
    hybrid_cuf = (
        total_mwh / ((spec.solar_ac_mw + spec.wind_ac_mw) * hours)
        if (spec.solar_ac_mw + spec.wind_ac_mw)
        else 0.0
    )
    specific_yield = solar_mwh / spec.solar_dc_mw if spec.solar_dc_mw else 0.0  # kWh/kWp/day
    return {
        "solar_mwh": solar_mwh,
        "wind_mwh": wind_mwh,
        "total_mwh": total_mwh,
        "solar_peak_mw": max((r.solar_mw for r in results), default=0.0),
        "wind_peak_mw": max((r.wind_mw for r in results), default=0.0),
        "total_peak_mw": max((r.total_mw for r in results), default=0.0),
        "solar_cuf": solar_cuf,
        "wind_cuf": wind_cuf,
        "hybrid_cuf": hybrid_cuf,
        "solar_specific_yield": specific_yield,
    }


def _persist_generation(
    db: Session,
    spec: PlantSpec,
    sim_date: date,
    mode: DataMode,
    results: list[BlockResult],
    quality: QualityReport,
    weather_source: str,
    weather_fetch_time: datetime,
    settings: Settings,
    is_reprocess: bool,
) -> None:
    sim_v = settings.SIMULATION_VERSION
    model_v = settings.MODEL_ASSUMPTION_VERSION
    base_label = MODE_LABEL[mode]
    summary_label = "REPROCESSED" if is_reprocess else base_label
    if quality.status == "FAILED":
        summary_label = "FAILED"

    now_utc = datetime.now(UTC)
    current_block = _current_block_no(spec.timezone, sim_date) if mode == DataMode.LIVE else 96

    # Versioning: demote all currently-current rows for this plant/date/mode, then
    # replace rows for THIS (sim_version, model_version) — preserving other versions.
    db.query(GenerationBlock).filter(
        GenerationBlock.plant_code == spec.plant_code,
        GenerationBlock.sim_date == sim_date,
        GenerationBlock.data_mode == mode.value,
        GenerationBlock.is_current.is_(True),
    ).update({GenerationBlock.is_current: False}, synchronize_session=False)
    db.query(GenerationBlock).filter(
        GenerationBlock.plant_code == spec.plant_code,
        GenerationBlock.sim_date == sim_date,
        GenerationBlock.data_mode == mode.value,
        GenerationBlock.simulation_version == sim_v,
        GenerationBlock.model_assumption_version == model_v,
    ).delete(synchronize_session=False)

    for r in results:
        # Per-block label: live future blocks are forecast.
        block_label = base_label
        weather_model = None
        forecast_generated_at = None
        if is_reprocess:
            block_label = "REPROCESSED"
        elif mode == DataMode.LIVE and r.block_no > current_block:
            block_label = "FORECAST_SIMULATED"
            weather_model = weather_source
            forecast_generated_at = weather_fetch_time
        elif mode == DataMode.FORECAST:
            weather_model = weather_source
            forecast_generated_at = weather_fetch_time

        db.add(
            GenerationBlock(
                plant_code=spec.plant_code,
                sim_date=sim_date,
                block_no=r.block_no,
                block_start=r.block_start,
                block_end=r.block_end,
                solar_mw=r.solar_mw,
                solar_mwh=r.solar_mwh,
                wind_mw=r.wind_mw,
                wind_mwh=r.wind_mwh,
                total_mw=r.total_mw,
                total_mwh=r.total_mwh,
                solar_cuf=r.solar_cuf,
                wind_cuf=r.wind_cuf,
                hybrid_cuf=r.hybrid_cuf,
                solar_status=r.solar_status,
                wind_status=r.wind_status,
                data_mode=mode.value,
                data_source=weather_source,
                data_label=block_label,
                data_quality_status=r.data_quality_status,
                simulation_version=sim_v,
                model_assumption_version=model_v,
                plant_config_version=spec.config_version,
                weather_source=weather_source,
                weather_fetch_time=weather_fetch_time,
                weather_model_used=weather_model,
                forecast_generated_at=forecast_generated_at,
                is_current=True,
                processed_at=now_utc,
            )
        )

    # Daily summary (same versioning rules).
    s = _summarize(spec, results)
    db.query(DailySummary).filter(
        DailySummary.plant_code == spec.plant_code,
        DailySummary.sim_date == sim_date,
        DailySummary.data_mode == mode.value,
        DailySummary.is_current.is_(True),
    ).update({DailySummary.is_current: False}, synchronize_session=False)
    db.query(DailySummary).filter(
        DailySummary.plant_code == spec.plant_code,
        DailySummary.sim_date == sim_date,
        DailySummary.data_mode == mode.value,
        DailySummary.simulation_version == sim_v,
        DailySummary.model_assumption_version == model_v,
    ).delete(synchronize_session=False)
    db.add(
        DailySummary(
            plant_code=spec.plant_code,
            sim_date=sim_date,
            data_mode=mode.value,
            data_label=summary_label,
            data_quality_status=quality.status,
            blocks_count=len(results),
            simulation_version=sim_v,
            model_assumption_version=model_v,
            plant_config_version=spec.config_version,
            weather_source=weather_source,
            is_current=True,
            processed_at=now_utc,
            **s,
        )
    )
    db.flush()


async def run_simulation(
    plant_code: str,
    sim_date: date,
    mode: DataMode | None = None,
    triggered_by: str = "manual",
    force_refetch: bool = False,
    settings: Settings | None = None,
) -> RunSummary:
    """Run one plant/date/mode simulation end-to-end and persist results."""
    settings = settings or get_settings()
    is_reprocess = triggered_by == "reprocess"

    with session_scope() as db:
        cfg = load_active_config(db, plant_code)
        spec = PlantSpec.from_orm(cfg)
        if mode is None:
            mode = resolve_mode(sim_date, spec.timezone)

        run = SimulationRun(
            plant_code=plant_code,
            sim_date=sim_date,
            data_mode=mode.value,
            status="OK",
            simulation_version=settings.SIMULATION_VERSION,
            triggered_by=triggered_by,
            started_at=datetime.now(UTC),
        )
        db.add(run)
        db.flush()
        run_id = run.id
        plantlike = _plantlike(spec)

    # Fetch (cache unless reprocess/force). LIVE/FORECAST are time-sensitive -> refetch.
    fetched_fresh = True
    cached_json = None
    weather_source = None
    fetched_at = None
    if not force_refetch and not is_reprocess and mode == DataMode.HISTORICAL:
        with session_scope() as db:
            cached = find_cached_raw(db, plant_code, sim_date, mode)
            if cached is not None:
                cached_json = cached.raw_json
                weather_source = cached.provider
                fetched_at = cached.fetched_at
                fetched_fresh = False

    try:
        if cached_json is not None:
            raw_json = cached_json
            # Re-derive a descriptive source label for the cached response.
            from app.weather.client import select_request

            _, _, weather_source = select_request(plantlike, sim_date, mode, settings)
        else:
            fetch = await fetch_weather(plantlike, sim_date, mode, settings)
            raw_json = fetch.json
            weather_source = fetch.weather_source
            fetched_at = fetch.fetched_at
            with session_scope() as db:
                persist_raw(db, fetch)

        blocks = normalize_to_blocks(raw_json, sim_date, spec.use_global_tilted_irradiance)
        results = simulate_day(spec, blocks)
        quality = check_day(spec, results)

        with session_scope() as db:
            persist_weather_blocks(
                db, plant_code, sim_date, mode, weather_source, blocks, fetched_at
            )
            _persist_generation(
                db, spec, sim_date, mode, results, quality,
                weather_source, fetched_at, settings, is_reprocess,
            )
            summary = _summarize(spec, results)
            run_status = "REPROCESSED" if is_reprocess else quality.status
            db.query(SimulationRun).filter(SimulationRun.id == run_id).update(
                {
                    "status": run_status,
                    "blocks_written": len(results),
                    "finished_at": datetime.now(UTC),
                    "message": "; ".join(quality.issues) if quality.issues else "ok",
                }
            )

        label = "REPROCESSED" if is_reprocess else MODE_LABEL[mode]
        if quality.status == "FAILED":
            label = "FAILED"
        logger.info(
            "Simulation done plant=%s date=%s mode=%s quality=%s blocks=%d",
            plant_code, sim_date, mode.value, quality.status, len(results),
        )
        return RunSummary(
            plant_code=plant_code,
            sim_date=sim_date,
            mode=mode.value,
            status=run_status,
            data_label=label,
            blocks_written=len(results),
            quality_status=quality.status,
            issues=quality.issues,
            weather_source=weather_source,
            fetched_fresh=fetched_fresh,
            solar_mwh=summary["solar_mwh"],
            wind_mwh=summary["wind_mwh"],
            total_mwh=summary["total_mwh"],
            solar_cuf=summary["solar_cuf"],
            wind_cuf=summary["wind_cuf"],
            hybrid_cuf=summary["hybrid_cuf"],
        )
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        logger.error("Simulation FAILED plant=%s date=%s: %s", plant_code, sim_date, exc)
        with session_scope() as db:
            db.add(
                ErrorLog(
                    context=f"run_simulation {plant_code} {sim_date} {mode.value if mode else '?'}",
                    message=str(exc),
                    traceback=tb,
                )
            )
            db.query(SimulationRun).filter(SimulationRun.id == run_id).update(
                {
                    "status": "FAILED",
                    "finished_at": datetime.now(UTC),
                    "message": str(exc),
                }
            )
        raise


def run_simulation_sync(
    plant_code: str,
    sim_date: date,
    mode: DataMode | None = None,
    triggered_by: str = "manual",
    force_refetch: bool = False,
    settings: Settings | None = None,
) -> RunSummary:
    return asyncio.run(
        run_simulation(plant_code, sim_date, mode, triggered_by, force_refetch, settings)
    )
