"""Day-ahead P90 schedule orchestration: anchor -> build -> freeze -> persist.

Freeze semantics matter here. A schedule is a published commitment for a date:
once issued it must not move when today's LIVE simulation re-runs every 15
minutes, otherwise the deviation being measured against it is a moving target
and the comparison is meaningless. So `ensure_schedule` is a no-op when a
current schedule already exists, unless explicitly forced.

Anchor selection, best-available first:
  * a future date  -> the FORECAST run (a genuine day-ahead anchor)
  * today / past   -> LIVE, else HISTORICAL, else FORECAST
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.repository import get_blocks, get_weather_blocks
from app.config.settings import Settings, get_settings
from app.db.base import session_scope
from app.db.models import GenerationBlock, ScheduleBlock
from app.engines.hybrid import BlockResult
from app.engines.p90 import ScheduleParams, build_schedule
from app.engines.spec import PlantSpec
from app.logging_conf import get_logger
from app.simulate import load_active_config
from app.weather.normalize import NormalizedBlock

logger = get_logger(__name__)

# Preference order when picking which simulation to anchor the schedule on.
_ANCHOR_ORDER_FUTURE = ("FORECAST", "LIVE", "HISTORICAL")
_ANCHOR_ORDER_PAST = ("LIVE", "HISTORICAL", "FORECAST")


class ScheduleError(RuntimeError):
    """No usable anchor simulation exists for the requested plant/date."""


def params_from(settings: Settings) -> ScheduleParams:
    return ScheduleParams(
        sigma_scale=settings.SCHEDULE_SIGMA_SCALE,
        rel_sigma_cap=settings.SCHEDULE_REL_SIGMA_CAP,
        day_energy_cap=settings.SCHEDULE_DAY_ENERGY_CAP,
    )


def _to_block_results(rows: list[GenerationBlock]) -> list[BlockResult]:
    """Adapt persisted generation rows to what the engine expects.

    Only the fields the P90 engine reads are populated; the rest carry neutral
    defaults so the dataclass contract holds.
    """
    return [
        BlockResult(
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
            interpolated=False,
            data_quality_status=r.data_quality_status,
        )
        for r in rows
    ]


def _to_normalized(rows) -> list[NormalizedBlock]:
    """Adapt persisted weather rows; only cloud + hub wind speed are consumed."""
    return [
        NormalizedBlock(
            block_no=w.block_no,
            block_start=w.block_start,
            block_end=w.block_end,
            interpolated=bool(w.interpolated),
            cloud_cover=w.cloud_cover,
            wind_speed_10m=w.wind_speed_10m,
            wind_speed_100m=w.wind_speed_100m,
        )
        for w in rows
    ]


def _pick_anchor(db: Session, plant_code: str, sim_date: date, tz: str):
    """Return (generation_rows, weather_rows, mode) for the best available anchor."""
    today = datetime.now(ZoneInfo(tz)).date()
    order = _ANCHOR_ORDER_FUTURE if sim_date > today else _ANCHOR_ORDER_PAST
    for mode in order:
        rows = get_blocks(db, plant_code, sim_date, mode)
        if rows:
            return rows, get_weather_blocks(db, plant_code, sim_date, mode), mode
    return None, None, None


def has_schedule(db: Session, plant_code: str, sim_date: date) -> bool:
    return db.scalar(
        select(ScheduleBlock.id).where(
            ScheduleBlock.plant_code == plant_code,
            ScheduleBlock.sim_date == sim_date,
            ScheduleBlock.is_current.is_(True),
        ).limit(1)
    ) is not None


def get_schedule(db: Session, plant_code: str, sim_date: date) -> list[ScheduleBlock]:
    return list(db.scalars(
        select(ScheduleBlock)
        .where(
            ScheduleBlock.plant_code == plant_code,
            ScheduleBlock.sim_date == sim_date,
            ScheduleBlock.is_current.is_(True),
        )
        .order_by(ScheduleBlock.block_no)
    ))


def get_schedule_range(
    db: Session, plant_code: str, start: date, end: date
) -> list[ScheduleBlock]:
    return list(db.scalars(
        select(ScheduleBlock)
        .where(
            ScheduleBlock.plant_code == plant_code,
            ScheduleBlock.sim_date >= start,
            ScheduleBlock.sim_date <= end,
            ScheduleBlock.is_current.is_(True),
        )
        .order_by(ScheduleBlock.sim_date, ScheduleBlock.block_no)
    ))


def ensure_schedule(
    plant_code: str,
    sim_date: date,
    force: bool = False,
    settings: Settings | None = None,
) -> dict:
    """Issue the day-ahead schedule for a date if it does not already exist.

    Idempotent and frozen by default: returns {"issued": False, "reason": "frozen"}
    when a current schedule is already published. Pass force=True to re-issue
    (the prior version is demoted, not deleted).
    """
    settings = settings or get_settings()
    if not settings.SCHEDULE_ENABLED:
        return {"issued": False, "reason": "disabled"}

    with session_scope() as db:
        cfg = load_active_config(db, plant_code)
        spec = PlantSpec.from_orm(cfg)
        tz = cfg.timezone

        if not force and has_schedule(db, plant_code, sim_date):
            return {"issued": False, "reason": "frozen", "plant_code": plant_code,
                    "sim_date": sim_date.isoformat()}

        gen_rows, wx_rows, mode = _pick_anchor(db, plant_code, sim_date, tz)
        if not gen_rows:
            raise ScheduleError(
                f"No simulation to anchor a schedule on for {plant_code} {sim_date}. "
                "Run a simulation for that date first."
            )

        blocks = build_schedule(
            spec,
            _to_block_results(gen_rows),
            _to_normalized(wx_rows or []),
            sim_date,
            params_from(settings),
        )

        # Preserve history: demote the prior version rather than deleting it.
        db.query(ScheduleBlock).filter(
            ScheduleBlock.plant_code == plant_code,
            ScheduleBlock.sim_date == sim_date,
            ScheduleBlock.is_current.is_(True),
        ).update({ScheduleBlock.is_current: False}, synchronize_session=False)
        db.query(ScheduleBlock).filter(
            ScheduleBlock.plant_code == plant_code,
            ScheduleBlock.sim_date == sim_date,
            ScheduleBlock.schedule_version == settings.SCHEDULE_VERSION,
        ).delete(synchronize_session=False)

        issued_at = datetime.now(UTC)
        for b in blocks:
            db.add(ScheduleBlock(
                plant_code=plant_code,
                sim_date=sim_date,
                block_no=b.block_no,
                block_start=b.block_start,
                block_end=b.block_end,
                solar_p90_mw=b.solar_p90_mw,
                wind_p90_mw=b.wind_p90_mw,
                total_p90_mw=b.total_p90_mw,
                solar_p90_mwh=b.solar_p90_mwh,
                wind_p90_mwh=b.wind_p90_mwh,
                total_p90_mwh=b.total_p90_mwh,
                solar_band_low_mw=b.solar_band_low_mw,
                solar_band_high_mw=b.solar_band_high_mw,
                wind_band_low_mw=b.wind_band_low_mw,
                wind_band_high_mw=b.wind_band_high_mw,
                total_band_low_mw=b.total_band_low_mw,
                total_band_high_mw=b.total_band_high_mw,
                anchor_mode=mode,
                schedule_version=settings.SCHEDULE_VERSION,
                plant_config_version=spec.config_version,
                issued_at=issued_at,
                is_current=True,
            ))

        total_mwh = sum(b.total_p90_mwh for b in blocks)
        logger.info(
            "Schedule issued plant=%s date=%s anchor=%s blocks=%d total=%.1fMWh",
            plant_code, sim_date, mode, len(blocks), total_mwh,
        )
        return {
            "issued": True, "plant_code": plant_code,
            "sim_date": sim_date.isoformat(), "anchor_mode": mode,
            "blocks": len(blocks),
            "solar_mwh": round(sum(b.solar_p90_mwh for b in blocks), 3),
            "wind_mwh": round(sum(b.wind_p90_mwh for b in blocks), 3),
            "total_mwh": round(total_mwh, 3),
            "issued_at": issued_at.isoformat(),
        }


def accuracy(db: Session, plant_code: str, sim_date: date) -> dict | None:
    """Schedule-vs-actual metrics for a date, or None if either side is missing.

    nMAE and the band counts are normalised to CAPACITY, which is how Indian DSM
    deviation is assessed. MAPE is reported only over blocks above 20% of
    capacity — at 06:00, when a solar plant is producing 1 MW, a percentage-of-
    actual error is arithmetically large and operationally meaningless.
    """
    sched = get_schedule(db, plant_code, sim_date)
    if not sched:
        return None
    cfg = load_active_config(db, plant_code)
    cap = cfg.solar_ac_mw + cfg.wind_ac_mw

    actual_rows = None
    for mode in _ANCHOR_ORDER_PAST:
        rows = get_blocks(db, plant_code, sim_date, mode)
        if rows:
            actual_rows = rows
            break
    if not actual_rows:
        return None

    amap = {r.block_no: r for r in actual_rows}
    pairs = [(amap[s.block_no].total_mw, s.total_p90_mw) for s in sched if s.block_no in amap]
    if not pairs:
        return None

    errs = [abs(f - a) for a, f in pairs]
    gen = [(a, f) for a, f in pairs if a > 0.20 * cap]
    act_mwh = sum(amap[s.block_no].total_mwh for s in sched if s.block_no in amap)
    sch_mwh = sum(s.total_p90_mwh for s in sched if s.block_no in amap)

    return {
        "blocks_compared": len(pairs),
        "nmae_pct_capacity": round(sum(errs) / len(errs) / cap * 100, 2),
        "mape_pct": round(
            sum(abs(f - a) / a for a, f in gen) / len(gen) * 100, 2) if gen else None,
        "blocks_within_10pct_band": round(
            sum(1 for e in errs if e <= 0.10 * cap) / len(errs) * 100, 1),
        "blocks_within_15pct_band": round(
            sum(1 for e in errs if e <= 0.15 * cap) / len(errs) * 100, 1),
        "actual_mwh": round(act_mwh, 3),
        "scheduled_mwh": round(sch_mwh, 3),
        "day_deviation_pct": round((sch_mwh - act_mwh) / act_mwh * 100, 2) if act_mwh else None,
    }
