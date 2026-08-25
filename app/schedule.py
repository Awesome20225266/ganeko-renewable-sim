"""Day-ahead P90 schedule orchestration: anchor -> build -> freeze -> persist.

Freeze semantics matter here, and they are directional. A schedule is a published
commitment for a date: the block we are currently inside, and every block before
it, must not move — otherwise the deviation being measured against it is a moving
target and the comparison is meaningless. Blocks still in the future are a
different matter: newer forecast information *should* reach them.

So `ensure_schedule` is a no-op when a current schedule exists and `force` is not
set, and a FORWARD-ONLY revision when it is. The hourly maintenance pass
(`run_schedule_retry`) supplies that force automatically, so revision needs no
human; `protected_schedule_block_nos` is what makes it safe to run repeatedly.

Anchor selection, best-available first:
  * a future date  -> the FORECAST run (a genuine day-ahead anchor)
  * today / past   -> LIVE, else HISTORICAL, else FORECAST
"""
from __future__ import annotations

from dataclasses import dataclass
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
from app.immutability import protected_schedule_block_nos
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


@dataclass
class _Anchor:
    """The simulation a schedule is built from, plus how old its weather is."""

    gen_rows: list[GenerationBlock]
    wx_rows: list
    mode: str
    age_hours: float | None


def anchor_age_hours(rows: list[GenerationBlock]) -> float | None:
    """Age of the newest weather behind these blocks, in hours (None if unknown).

    Reads weather_fetch_time rather than processed_at: a rate-limited run re-simulates
    from stored weather and stamps a *new* processed_at while the underlying observation
    stays old, so processed_at would report every stale anchor as fresh.
    """
    times = [r.weather_fetch_time for r in rows if r.weather_fetch_time is not None]
    if not times:
        return None
    newest = max(times)
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=UTC)
    return (datetime.now(UTC) - newest).total_seconds() / 3600.0


def _pick_anchor(
    db: Session,
    plant_code: str,
    sim_date: date,
    tz: str,
    max_age_hours: float | None = None,
) -> tuple[_Anchor | None, list[str]]:
    """Best usable anchor for this date, plus a list of candidates rejected as stale.

    Candidates are tried in preference order and any whose weather is older than
    `max_age_hours` is SKIPPED rather than accepted, so a fresher lower-preference
    anchor can win. Publishing off a stale anchor is the failure mode that hid a
    seven-night provider outage: the forecast step kept "succeeding" from week-old
    stored weather, so every schedule looked healthy while being anchored on a forecast
    that no longer described the day. If nothing fresh exists the caller raises instead.

    `sim_date >= today` uses the FUTURE order so that today's own day-ahead FORECAST
    anchor still wins over a LIVE run made the same morning. With `>` a recovery attempt
    at 00:30 local time would silently anchor a "day-ahead" commitment on that day's
    LIVE simulation and record anchor_mode=LIVE.
    """
    today = datetime.now(ZoneInfo(tz)).date()
    order = _ANCHOR_ORDER_FUTURE if sim_date >= today else _ANCHOR_ORDER_PAST
    stale: list[str] = []
    for mode in order:
        rows = get_blocks(db, plant_code, sim_date, mode)
        if not rows:
            continue
        age = anchor_age_hours(rows)
        if max_age_hours is not None and age is not None and age > max_age_hours:
            stale.append(f"{mode} ({age:.0f}h old)")
            continue
        return _Anchor(
            gen_rows=rows,
            wx_rows=get_weather_blocks(db, plant_code, sim_date, mode),
            mode=mode,
            age_hours=age,
        ), stale
    return None, stale


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

    A forced re-issue is **forward-only**: blocks up to and including the one
    containing now keep the values they were published with, and only later blocks
    take the new numbers. So a revision at 13:50 leaves 13:45-14:00 alone and takes
    effect from 14:00-14:15. See `app.immutability.protected_schedule_block_nos`.
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

        anchor, stale = _pick_anchor(
            db, plant_code, sim_date, tz, settings.SCHEDULE_MAX_ANCHOR_AGE_HOURS
        )
        if anchor is None:
            if stale:
                raise ScheduleError(
                    f"Only stale anchors available for {plant_code} {sim_date}: "
                    f"{', '.join(stale)} — max allowed is "
                    f"{settings.SCHEDULE_MAX_ANCHOR_AGE_HOURS}h. The weather provider is "
                    "most likely rate-limited; refusing to publish a day-ahead schedule "
                    "anchored on stale weather."
                )
            raise ScheduleError(
                f"No simulation to anchor a schedule on for {plant_code} {sim_date}. "
                "Run a simulation for that date first."
            )
        mode = anchor.mode

        blocks = build_schedule(
            spec,
            _to_block_results(anchor.gen_rows),
            _to_normalized(anchor.wx_rows or []),
            sim_date,
            params_from(settings),
        )

        # --- Forward-only revision -------------------------------------------
        # A revision may only move blocks whose operational time has not arrived.
        # Blocks 1..X (X = the block containing now) keep the values they were
        # published with, so the commitment the deviation is measured against
        # never moves under the operator's feet. A past date protects all 96; a
        # future date protects none. Enforced here, in the single writer of
        # `schedule_block`, so the daily job, the hourly retry and the CLI all
        # inherit it.
        # Column select rather than get_schedule(): ORM entities would linger in the
        # identity map after the bulk DELETE below and clash with the re-inserted
        # rows. Plain Rows carry the values without that side effect.
        prior = {
            r.block_no: r
            for r in db.execute(
                select(
                    ScheduleBlock.block_no,
                    ScheduleBlock.block_start,
                    ScheduleBlock.block_end,
                    ScheduleBlock.solar_p90_mw,
                    ScheduleBlock.wind_p90_mw,
                    ScheduleBlock.total_p90_mw,
                    ScheduleBlock.solar_p90_mwh,
                    ScheduleBlock.wind_p90_mwh,
                    ScheduleBlock.total_p90_mwh,
                    ScheduleBlock.solar_band_low_mw,
                    ScheduleBlock.solar_band_high_mw,
                    ScheduleBlock.wind_band_low_mw,
                    ScheduleBlock.wind_band_high_mw,
                    ScheduleBlock.total_band_low_mw,
                    ScheduleBlock.total_band_high_mw,
                    ScheduleBlock.anchor_mode,
                    ScheduleBlock.plant_config_version,
                    ScheduleBlock.issued_at,
                ).where(
                    ScheduleBlock.plant_code == plant_code,
                    ScheduleBlock.sim_date == sim_date,
                    ScheduleBlock.is_current.is_(True),
                )
            )
        }
        protected = protected_schedule_block_nos(
            sim_date, [b.block_no for b in blocks], tz, settings
        ) & prior.keys()

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
            if b.block_no in protected:
                # Republish the committed values verbatim, keeping the original
                # issue time and anchor. `/schedule` reports rows[0].issued_at,
                # which therefore keeps meaning "when this commitment was first
                # published" rather than "when it was last touched".
                p = prior[b.block_no]
                db.add(ScheduleBlock(
                    plant_code=plant_code,
                    sim_date=sim_date,
                    block_no=p.block_no,
                    block_start=p.block_start,
                    block_end=p.block_end,
                    solar_p90_mw=p.solar_p90_mw,
                    wind_p90_mw=p.wind_p90_mw,
                    total_p90_mw=p.total_p90_mw,
                    solar_p90_mwh=p.solar_p90_mwh,
                    wind_p90_mwh=p.wind_p90_mwh,
                    total_p90_mwh=p.total_p90_mwh,
                    solar_band_low_mw=p.solar_band_low_mw,
                    solar_band_high_mw=p.solar_band_high_mw,
                    wind_band_low_mw=p.wind_band_low_mw,
                    wind_band_high_mw=p.wind_band_high_mw,
                    total_band_low_mw=p.total_band_low_mw,
                    total_band_high_mw=p.total_band_high_mw,
                    anchor_mode=p.anchor_mode,
                    schedule_version=settings.SCHEDULE_VERSION,
                    plant_config_version=p.plant_config_version,
                    issued_at=p.issued_at,
                    is_current=True,
                ))
                continue
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

        # Totals over what was PUBLISHED (protected blocks kept their old values),
        # so the reported day energy matches the blocks it is made of.
        effective = [prior[b.block_no] if b.block_no in protected else b for b in blocks]
        total_mwh = sum(b.total_p90_mwh for b in effective)
        age = anchor.age_hours
        logger.info(
            "Schedule issued plant=%s date=%s anchor=%s anchor_age=%s blocks=%d "
            "revised=%d protected=%d total=%.1fMWh",
            plant_code, sim_date, mode,
            f"{age:.1f}h" if age is not None else "unknown",
            len(blocks), len(blocks) - len(protected), len(protected), total_mwh,
        )
        return {
            "issued": True, "plant_code": plant_code,
            "sim_date": sim_date.isoformat(), "anchor_mode": mode,
            "anchor_age_hours": round(age, 2) if age is not None else None,
            "blocks": len(blocks),
            # Forward-only revision accounting: how much of the day this call was
            # actually allowed to move.
            "blocks_revised": len(blocks) - len(protected),
            "blocks_protected": len(protected),
            "solar_mwh": round(sum(b.solar_p90_mwh for b in effective), 3),
            "wind_mwh": round(sum(b.wind_p90_mwh for b in effective), 3),
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
