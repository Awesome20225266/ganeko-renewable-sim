"""Simulation orchestrator: fetch -> raw store -> normalize -> engines -> persist.

Handles caching (avoid duplicate fetches unless reprocess), versioning (preserve
history by demoting prior `is_current` rows), per-block live/forecast labelling,
data-quality enforcement, and run/error logging.
"""
from __future__ import annotations

import asyncio
import traceback
from dataclasses import dataclass, replace
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
from app.immutability import (
    DIAGNOSTIC_BLOCK_LABEL,
    current_block_no,
    diagnostic_version,
    frozen_actual_block_nos,
)
from app.logging_conf import get_logger
from app.quality import QualityReport, check_day
from app.weather.client import (
    DataMode,
    PlantLike,
    WeatherFetchError,
    fetch_weather,
    resolve_mode,
)
from app.weather.normalize import normalize_to_blocks
from app.weather.store import (
    find_cached_raw,
    find_raw_covering,
    persist_raw,
    persist_weather_blocks,
)

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
    # True when the provider could not be reached and we simulated from a previously
    # stored response. The data is real and usable, but the provider is still down —
    # callers use this to keep backing off instead of treating the run as a clean success.
    weather_from_cache: bool = False


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
    """Block containing 'now' in plant-local time.

    Thin alias: the interval convention lives in `app.immutability` so the
    labelling boundary here and the schedule-protection boundary there can never
    drift apart.
    """
    return current_block_no(tz, sim_date)


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


def _published_blocks(
    db: Session,
    plant_code: str,
    sim_date: date,
    data_mode: str,
    sim_v: str,
    model_v: str,
) -> dict[int, object]:
    """Currently-published rows for one plant/date/mode/version, keyed by block_no.

    A column select, not an ORM query, on purpose. Loading GenerationBlock entities
    would put them in the session's identity map, and the bulk DELETE in
    `_persist_generation` (synchronize_session=False) leaves those instances stale —
    then the re-inserted rows reuse the same primary keys and SQLAlchemy warns about
    a clashing identity. Plain Rows never enter the identity map.
    """
    return {
        row.block_no: row
        for row in db.execute(
            select(
                GenerationBlock.block_no,
                GenerationBlock.data_label,
                GenerationBlock.solar_mw,
                GenerationBlock.solar_mwh,
                GenerationBlock.wind_mw,
                GenerationBlock.wind_mwh,
                GenerationBlock.total_mw,
                GenerationBlock.total_mwh,
                GenerationBlock.solar_cuf,
                GenerationBlock.wind_cuf,
                GenerationBlock.hybrid_cuf,
                GenerationBlock.solar_status,
                GenerationBlock.wind_status,
                GenerationBlock.data_quality_status,
            ).where(
                GenerationBlock.plant_code == plant_code,
                GenerationBlock.sim_date == sim_date,
                GenerationBlock.data_mode == data_mode,
                GenerationBlock.simulation_version == sim_v,
                GenerationBlock.model_assumption_version == model_v,
                GenerationBlock.is_current.is_(True),
            )
        )
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
) -> list[BlockResult]:
    """Persist one day of generation. Returns the results as actually PUBLISHED.

    The return value matters: an already-published Actual is carried forward
    unchanged, so the freshly simulated `results` are not necessarily what ends up
    in the table. Callers must summarise/report the returned list, not their input,
    or the daily totals would disagree with the blocks they are made of.
    """
    published_sim_v = settings.SIMULATION_VERSION
    model_v = settings.MODEL_ASSUMPTION_VERSION
    base_label = MODE_LABEL[mode]

    # --- Administrative re-run = diagnostic, NOT a republished Actual ---------
    # An admin reprocess must never displace what consumers are being served. It
    # writes into its own version space with is_current=False, so:
    #   * uq_generation_block / uq_daily_summary cannot collide with the published
    #     rows (both constraints include the two version columns);
    #   * every read helper filters is_current=True, so no API returns it;
    #   * the published rows are left completely untouched — see the demote step,
    #     which is skipped entirely below.
    # The values are stored and inspectable for diagnosis, which is the point.
    write_sim_v = diagnostic_version(published_sim_v) if is_reprocess else published_sim_v
    row_is_current = not is_reprocess

    summary_label = DIAGNOSTIC_BLOCK_LABEL if is_reprocess else base_label
    if quality.status == "FAILED":
        summary_label = "FAILED"

    now_utc = datetime.now(UTC)
    current_block = _current_block_no(spec.timezone, sim_date) if mode == DataMode.LIVE else 96

    # --- Actual immutability -------------------------------------------------
    # Read what is already published BEFORE the delete below, so an Actual that has
    # already been served can be carried forward instead of recomputed.
    #
    # Enforced here, in the single writer of `generation_block`, rather than in any
    # route or job: /live, /current, the wrapper, the dashboard, the live-refresh
    # job, the daily job and the CLI all reach the table through this function, so
    # no caller can bypass the invariant by accident.
    frozen: set[int] = set()
    prior: dict[int, object] = {}
    # Blocks whose stored label should be reused verbatim. Only same-mode rows
    # qualify: an inherited LIVE value republished into the HISTORICAL series must
    # still be labelled HISTORICAL_SIMULATED, because the label names the series a
    # consumer asked for, not where the number originally came from.
    same_mode: set[int] = set()
    inherited = 0
    if not is_reprocess:
        prior = _published_blocks(
            db, spec.plant_code, sim_date, mode.value, published_sim_v, model_v
        )
        same_mode = set(prior)

        if mode == DataMode.HISTORICAL:
            # One Actual per interval, across the whole lifecycle. A day that was
            # served live already has an Actual of record; when it later becomes
            # historical, that number must be REPUBLISHED, not recomputed from the
            # archive. Without this the value a consumer reads would change once, at
            # the LIVE->HISTORICAL handover — measured on real production data for
            # 2026-08-24: 55 of 96 blocks differed, up to -24 MW on a single block
            # and -3.13% on day energy.
            #
            # Same-mode rows win: a HISTORICAL Actual already published is itself
            # immutable, so it is never replaced by the LIVE series either.
            for no, row in _published_blocks(
                db, spec.plant_code, sim_date, DataMode.LIVE.value,
                published_sim_v, model_v,
            ).items():
                if no not in prior:
                    prior[no] = row
                    inherited += 1

        frozen = frozen_actual_block_nos(
            {no: row.data_label for no, row in prior.items()}, settings
        )
        if frozen:
            logger.info(
                "Immutable actuals preserved plant=%s date=%s mode=%s blocks=%d/%d"
                " (inherited from LIVE: %d)",
                spec.plant_code, sim_date, mode.value, len(frozen), len(results),
                len(frozen & (set(prior) - same_mode)),
            )

    # Versioning: demote all currently-current rows for this plant/date/mode, then
    # replace rows for THIS (sim_version, model_version) — preserving other versions.
    #
    # A diagnostic run skips the demote entirely: it must leave the published rows
    # exactly as they are, is_current and all. It only ever replaces its own
    # previous diagnostic (write_sim_v is the reprocess version space).
    if not is_reprocess:
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
        GenerationBlock.simulation_version == write_sim_v,
        GenerationBlock.model_assumption_version == model_v,
    ).delete(synchronize_session=False)

    published: list[BlockResult] = []
    for r in results:
        # Per-block label: live future blocks are forecast.
        block_label = base_label
        weather_model = None
        forecast_generated_at = None
        if is_reprocess:
            block_label = DIAGNOSTIC_BLOCK_LABEL
        elif mode == DataMode.LIVE and r.block_no > current_block:
            block_label = "FORECAST_SIMULATED"
            weather_model = weather_source
            forecast_generated_at = weather_fetch_time
        elif mode == DataMode.FORECAST:
            weather_model = weather_source
            forecast_generated_at = weather_fetch_time

        if r.block_no in frozen:
            # Already published as an Actual: republish the stored numbers verbatim
            # and keep the original label. Only the physical values and their
            # quality verdict are carried over — the run/lineage columns below
            # (weather_source, weather_fetch_time, processed_at) still describe the
            # run that wrote this row, which is both honest and required: the
            # freshness gate in `_latest_live_times` reads weather_fetch_time off
            # the newest row, and if frozen rows reported an old fetch time then
            # late in the day, once all 96 blocks are frozen, every single read
            # would look stale and launch another provider call.
            p = prior[r.block_no]
            r = replace(
                r,
                solar_mw=p.solar_mw, solar_mwh=p.solar_mwh,
                wind_mw=p.wind_mw, wind_mwh=p.wind_mwh,
                total_mw=p.total_mw, total_mwh=p.total_mwh,
                solar_cuf=p.solar_cuf, wind_cuf=p.wind_cuf, hybrid_cuf=p.hybrid_cuf,
                solar_status=p.solar_status, wind_status=p.wind_status,
                data_quality_status=p.data_quality_status,
            )
            if r.block_no in same_mode:
                # Republishing within the same series: keep the stored label, so a
                # published Actual can never be downgraded to a forecast label.
                block_label = p.data_label
        published.append(r)

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
                simulation_version=write_sim_v,
                model_assumption_version=model_v,
                plant_config_version=spec.config_version,
                weather_source=weather_source,
                weather_fetch_time=weather_fetch_time,
                weather_model_used=weather_model,
                forecast_generated_at=forecast_generated_at,
                is_current=row_is_current,
                processed_at=now_utc,
            )
        )

    # Daily summary (same versioning rules). Built from `published`, not `results`:
    # with frozen blocks in play they differ, and a total that disagreed with the
    # sum of its own blocks would be worse than either number alone.
    s = _summarize(spec, published)
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
        DailySummary.simulation_version == write_sim_v,
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
            simulation_version=write_sim_v,
            model_assumption_version=model_v,
            plant_config_version=spec.config_version,
            weather_source=weather_source,
            is_current=row_is_current,
            processed_at=now_utc,
            **s,
        )
    )
    db.flush()
    return published


def _load_fallback_raw(
    plant_code: str, sim_date: date
) -> tuple[dict | None, str | None, datetime | None]:
    """Newest stored provider response covering `sim_date`, as plain (json, label, fetched_at).

    Values are copied out inside the session so nothing detached is touched later.
    """
    try:
        with session_scope() as db:
            row = find_raw_covering(db, plant_code, sim_date)
            if row is None:
                return None, None, None
            return dict(row.raw_json or {}), f"{row.provider}:stored", row.fetched_at
    except Exception as exc:  # noqa: BLE001 — fallback lookup must never mask the real error
        logger.warning("fallback weather lookup failed for %s %s: %s", plant_code, sim_date, exc)
        return None, None, None


async def run_simulation(
    plant_code: str,
    sim_date: date,
    mode: DataMode | None = None,
    triggered_by: str = "manual",
    force_refetch: bool = False,
    settings: Settings | None = None,
    allow_network: bool = True,
) -> RunSummary:
    """Run one plant/date/mode simulation end-to-end and persist results.

    `allow_network=False` re-simulates purely from already-stored weather (no provider
    call at all) — used to advance today's block labels while the provider is rate-limiting
    us. If the provider call fails, we fall back to stored weather automatically rather
    than leaving the date with no data.
    """
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

    weather_from_cache = False
    try:
        if cached_json is not None:
            raw_json = cached_json
            # Re-derive a descriptive source label for the cached response.
            from app.weather.client import select_request

            _, _, weather_source = select_request(plantlike, sim_date, mode, settings)
        else:
            fetch = None
            fetch_error: Exception | None = None
            if allow_network:
                try:
                    fetch = await fetch_weather(plantlike, sim_date, mode, settings)
                except WeatherFetchError as exc:
                    fetch_error = exc
            if fetch is not None:
                raw_json = fetch.json
                weather_source = fetch.weather_source
                fetched_at = fetch.fetched_at
                with session_scope() as db:
                    persist_raw(db, fetch)
            else:
                # Provider unreachable / rate-limiting (or deliberately skipped). Simulate
                # from the newest stored response that covers this date so the day still
                # has data, instead of failing and leaving consumers with nothing.
                raw_json, weather_source, fetched_at = _load_fallback_raw(plant_code, sim_date)
                if raw_json is None:
                    raise fetch_error or WeatherFetchError(
                        f"No stored weather available for {plant_code} {sim_date}"
                    )
                weather_from_cache = True
                fetched_fresh = False
                logger.warning(
                    "Simulating plant=%s date=%s mode=%s from STORED weather (%s): %s",
                    plant_code, sim_date, mode.value, weather_source,
                    fetch_error or "network skipped",
                )

        blocks = normalize_to_blocks(
            raw_json,
            sim_date,
            spec.use_global_tilted_irradiance,
            latitude=spec.latitude,
            longitude=spec.longitude,
            timezone=spec.timezone,
        )
        results = simulate_day(spec, blocks, texture=settings.REALISM_TEXTURE)
        quality = check_day(spec, results)

        with session_scope() as db:
            persist_weather_blocks(
                db, plant_code, sim_date, mode, weather_source, blocks, fetched_at
            )
            published = _persist_generation(
                db, spec, sim_date, mode, results, quality,
                weather_source, fetched_at, settings, is_reprocess,
            )
            # Report what was published, not what was simulated — frozen actuals
            # mean the two can differ, and the caller (CLI, /admin/reprocess,
            # dashboard) is asking what the API will now serve.
            summary = _summarize(spec, published)
            run_status = "REPROCESSED" if is_reprocess else quality.status
            message = "; ".join(quality.issues) if quality.issues else "ok"
            if weather_from_cache:
                message = f"{message} (stored weather from {fetched_at:%Y-%m-%d %H:%MZ})"
            db.query(SimulationRun).filter(SimulationRun.id == run_id).update(
                {
                    "status": run_status,
                    "blocks_written": len(results),
                    "finished_at": datetime.now(UTC),
                    "message": message[:500],
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
            weather_from_cache=weather_from_cache,
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
    allow_network: bool = True,
) -> RunSummary:
    return asyncio.run(
        run_simulation(
            plant_code, sim_date, mode, triggered_by, force_refetch, settings, allow_network
        )
    )


# --------------------------------------------------------------------------- #
# On-access freshness: keep today's LIVE simulation no older than the refresh
# window, so a shared API key always returns current data even when the
# scheduler isn't running. The per-plant lock + freshness gate ensure at most
# one re-fetch per window no matter how many consumers poll concurrently.
# --------------------------------------------------------------------------- #
import threading  # noqa: E402
from collections import defaultdict  # noqa: E402

_live_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)

# Per-plant provider-failure memory. Without it, a failed refresh leaves the stored
# weather_fetch_time untouched, so the freshness gate below never engages and EVERY
# subsequent read launches another provider call — turning one rate-limit response into
# a self-sustaining outage (measured: 240 provider calls/hour). Escalating backoff caps
# an outage at ~2 calls/hour instead.
_live_failures: dict[str, tuple[datetime, int]] = {}
_FAILURE_BACKOFF_MINUTES = (2, 5, 15, 30)


def _failure_backoff_seconds(plant_code: str) -> tuple[float, float] | None:
    """(remaining, backoff) seconds if this plant is inside its provider-failure backoff."""
    entry = _live_failures.get(plant_code)
    if entry is None:
        return None
    failed_at, count = entry
    idx = min(count, len(_FAILURE_BACKOFF_MINUTES)) - 1
    backoff = _FAILURE_BACKOFF_MINUTES[idx] * 60.0
    elapsed = (datetime.now(UTC) - failed_at).total_seconds()
    return (backoff - elapsed, backoff) if elapsed < backoff else None


def _record_live_failure(plant_code: str) -> None:
    prev = _live_failures.get(plant_code)
    _live_failures[plant_code] = (datetime.now(UTC), (prev[1] + 1) if prev else 1)


def _clear_live_failure(plant_code: str) -> None:
    _live_failures.pop(plant_code, None)


def _latest_live_times(
    db: Session, plant_code: str, sim_date: date
) -> tuple[datetime | None, datetime | None]:
    """(weather_fetch_time, processed_at) of today's newest current LIVE block."""
    row = db.scalar(
        select(GenerationBlock)
        .where(
            GenerationBlock.plant_code == plant_code,
            GenerationBlock.sim_date == sim_date,
            GenerationBlock.data_mode == DataMode.LIVE.value,
            GenerationBlock.is_current.is_(True),
        )
        .order_by(GenerationBlock.processed_at.desc())
    )
    if row is None:
        return None, None

    def _aware(t: datetime | None) -> datetime | None:
        if t is not None and t.tzinfo is None:
            return t.replace(tzinfo=UTC)
        return t

    return _aware(row.weather_fetch_time or row.processed_at), _aware(row.processed_at)


def _latest_live_fetch_time(db: Session, plant_code: str, sim_date: date) -> datetime | None:
    return _latest_live_times(db, plant_code, sim_date)[0]


def ensure_fresh_live(plant_code: str, max_age_minutes: int | None = None) -> dict:
    """Refresh today's LIVE simulation if it is older than the freshness window.

    Returns {"refreshed": bool, "age_seconds": float|None, "as_of": iso|None}.
    Safe to call on every API/dashboard read; it re-simulates at most once per window.
    """
    settings = get_settings()
    max_age = (max_age_minutes or settings.LIVE_REFRESH_MINUTES) * 60.0
    # Only a run that actually called the provider may touch the backoff state; a
    # stored-weather-only run must never slide the timer, or the provider would never
    # be retried and the plant would stay on stale weather forever.
    attempted_network = False
    try:
        with session_scope() as db:
            cfg = load_active_config(db, plant_code)
            tz = cfg.timezone
            today = datetime.now(ZoneInfo(tz)).date()
            last, processed = _latest_live_times(db, plant_code, today)
        now = datetime.now(UTC)
        age = (now - last).total_seconds() if last else None
        if age is not None and age < max_age:
            return {"refreshed": False, "age_seconds": age, "as_of": last.isoformat()}

        # The weather we have is stale (or missing). Decide whether we're allowed to ask
        # the provider, or must work with what is already stored.
        backoff = _failure_backoff_seconds(plant_code)
        sim_age = (now - processed).total_seconds() if processed else None
        if backoff is not None and sim_age is not None and sim_age < max_age:
            # Provider is down AND today's blocks were re-simulated recently — nothing to
            # do. This is the path that used to launch a provider call on every read.
            return {"refreshed": False, "age_seconds": age, "provider_backoff": True,
                    "retry_in_seconds": round(backoff[0]),
                    "as_of": last.isoformat() if last else None}

        lock = _live_locks[plant_code]
        if not lock.acquire(blocking=False):
            # Another refresh is already running; serve whatever is current.
            return {"refreshed": False, "age_seconds": age, "in_progress": True,
                    "as_of": last.isoformat() if last else None}
        try:
            # Re-check inside the lock (another thread may have just refreshed).
            with session_scope() as db:
                last2, _ = _latest_live_times(db, plant_code, today)
            age2 = (datetime.now(UTC) - last2).total_seconds() if last2 else None
            if age2 is not None and age2 < max_age:
                return {"refreshed": False, "age_seconds": age2, "as_of": last2.isoformat()}
            # Inside a provider backoff we still re-simulate, but strictly from stored
            # weather (allow_network=False, zero provider calls) so today's block labels
            # keep advancing from FORECAST_SIMULATED to LIVE_ESTIMATED as the day passes.
            attempted_network = backoff is None
            summary = run_simulation_sync(
                plant_code, today, DataMode.LIVE, triggered_by="auto-live",
                force_refetch=True, allow_network=attempted_network,
            )
            if attempted_network:
                if summary.weather_from_cache:
                    # Data was written, but the provider is still unavailable — keep (and
                    # escalate) the backoff rather than reporting a clean success.
                    _record_live_failure(plant_code)
                else:
                    _clear_live_failure(plant_code)
            with session_scope() as db:
                fresh, _ = _latest_live_times(db, plant_code, today)
            return {"refreshed": True, "age_seconds": 0.0,
                    "stale_weather": summary.weather_from_cache,
                    "as_of": fresh.isoformat() if fresh else None}
        finally:
            lock.release()
    except Exception as exc:  # noqa: BLE001 — never let a refresh failure break a read
        if attempted_network:
            _record_live_failure(plant_code)
        logger.warning("ensure_fresh_live(%s) failed: %s", plant_code, exc)
        return {"refreshed": False, "error": str(exc)}
