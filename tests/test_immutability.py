"""Publication invariants: Actuals never move, Schedule revises forward only.

Every test here is a business acceptance criterion, not an implementation detail:

  * an Actual that has been published survives a weather revision unchanged;
  * a Historical Actual survives a re-run unchanged;
  * a forecast block (today's not-yet-reached blocks) is still free to move;
  * a Schedule revision leaves the current and past blocks alone and moves X+1 on;
  * the 15:00 IST cutover applies to blocks at/after it and to nothing before;
  * `/admin/reprocess` remains the one deliberate way to restate an Actual.

No network is used: the provider is replaced by a synthetic response whose
irradiance can be dialled up between runs, which is exactly the real failure mode
(Open-Meteo revising an already-elapsed interval upward by 19-56%).
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# Isolated test DB BEFORE importing app modules.
_TMPDIR = tempfile.mkdtemp(prefix="rensim_immutable_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDIR}/test.db".replace("\\", "/")
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["ADMIN_BOOTSTRAP_KEY"] = "test-admin-key"
os.environ["PLANT_CODE"] = "HYBRID01"
os.environ["PLANT_TZ"] = "Asia/Kolkata"
os.environ["RENEWABLE_PLANT_ID"] = "HYBRID01"
os.environ["RENEWABLE_PLANT_TZ"] = "Asia/Kolkata"
# Blank = enforce for every block, which is the documented behaviour for a fresh
# environment with no pre-cutover rows to protect. The cutover boundary itself is
# tested explicitly further down with a real timestamp.
os.environ["GENERATION_IMMUTABILITY_CUTOVER"] = ""

import pytest  # noqa: E402

import app.config.settings as settings_mod  # noqa: E402
import app.db.base as db_base  # noqa: E402

settings_mod.get_settings.cache_clear()
db_base._engine = None
db_base._SessionLocal = None

import app.simulate as simulate_mod  # noqa: E402
import app.weather.client as wx_client  # noqa: E402
from app.db.base import session_scope  # noqa: E402
from app.db.models import GenerationBlock, ScheduleBlock  # noqa: E402
from app.db.seed import run_seed  # noqa: E402
from app.immutability import (  # noqa: E402
    ACTUAL_BLOCK_LABELS,
    DIAGNOSTIC_BLOCK_LABEL,
    _parse_cutover,
    current_block_no,
    diagnostic_version,
    first_revisable_schedule_block_no,
    is_enforced,
    is_final_actual,
    protected_schedule_block_nos,
    schedule_lock_blocks,
)
from app.schedule import ensure_schedule, get_schedule  # noqa: E402
from app.simulate import run_simulation_sync  # noqa: E402
from app.weather.client import DataMode  # noqa: E402

# A plant of this module's own. `db_base._engine` is a process-global singleton, so
# every test module in a full-suite run ends up sharing ONE database; writing this
# module's generation and schedule rows under HYBRID01 would collide with the dates
# other modules seed (uq_generation_block) and would leave today's LIVE rows fresh,
# breaking the freshness assertions in test_rate_limit_resilience. A dedicated plant
# keeps this module's writes entirely to itself.
PLANT = "IMMUT01"
TZ = "Asia/Kolkata"
IST = ZoneInfo(TZ)


# --- synthetic provider ------------------------------------------------------
def _raw(sim_date: date, ghi_scale: float) -> dict:
    """A well-formed hourly Open-Meteo response spanning the day, scalable.

    `ghi_scale` stands in for the provider revising its own past: the same
    interval comes back brighter or darker on a later fetch.
    """
    times: list[str] = []
    t = datetime.combine(sim_date - timedelta(days=1), time.min)
    last = datetime.combine(sim_date + timedelta(days=1), time(23, 0))
    while t <= last:
        times.append(t.isoformat(timespec="minutes"))
        t += timedelta(hours=1)

    def solar(ts: str) -> float:
        hour = int(ts[11:13])
        if 6 <= hour <= 18:
            import math
            return 900.0 * ghi_scale * max(0.0, math.sin(math.pi * (hour - 6) / 12))
        return 0.0

    n = len(times)
    return {
        "timezone": TZ,
        "hourly": {
            "time": times,
            "shortwave_radiation": [solar(x) for x in times],
            "direct_radiation": [solar(x) * 0.7 for x in times],
            "diffuse_radiation": [solar(x) * 0.3 for x in times],
            "direct_normal_irradiance": [solar(x) * 0.8 for x in times],
            "temperature_2m": [30.0] * n,
            "cloud_cover": [20.0] * n,
            "is_day": [1 if 6 <= int(x[11:13]) <= 18 else 0 for x in times],
            "wind_speed_10m": [6.0] * n,
            "wind_speed_100m": [8.0 * ghi_scale] * n,
            "wind_speed_120m": [8.2] * n,
            "wind_speed_180m": [8.5] * n,
            "wind_direction_100m": [180.0] * n,
            "wind_gusts_10m": [11.0] * n,
            "surface_pressure": [950.0] * n,
        },
    }


@pytest.fixture(autouse=True, scope="module")
def _seeded():
    """Seed the baseline plant, then clone its config under this module's plant code."""
    from sqlalchemy import inspect, select

    from app.db.models import Plant, PlantConfig

    run_seed()
    with session_scope() as db:
        if db.scalar(select(Plant).where(Plant.plant_code == PLANT)) is None:
            base = db.scalar(
                select(PlantConfig)
                .where(PlantConfig.is_active.is_(True))
                .order_by(PlantConfig.config_version.desc())
            )
            assert base is not None, "run_seed() did not create a plant config"
            plant = Plant(
                plant_code=PLANT, plant_name="Immutability Test Plant",
                active_config_version=1,
            )
            db.add(plant)
            db.flush()
            skip = {
                "id", "plant_id", "plant_code", "plant_name", "created_at",
                # Never clone a forward-dated cutover: this plant must have a config
                # in force on every date it simulates, whatever another module left
                # active on the shared database.
                "effective_from_date",
            }
            fields = {
                c.key: getattr(base, c.key)
                for c in inspect(PlantConfig).mapper.column_attrs
                if c.key not in skip
            }
            fields["config_version"] = 1
            fields["timezone"] = TZ
            db.add(PlantConfig(
                plant_id=plant.id, plant_code=PLANT,
                plant_name="Immutability Test Plant", **fields,
            ))
    yield


@pytest.fixture
def provider(monkeypatch):
    """Patch the weather fetch; `provider.scale` controls the 'revision'."""

    class _P:
        scale = 1.0

    p = _P()

    async def fake_fetch(plant, sim_date, mode, settings=None, today=None):
        return wx_client.RawFetch(
            plant_code=plant.plant_code,
            sim_date=sim_date,
            mode=mode,
            provider="open-meteo",
            weather_source="test:synthetic",
            request_url="test://synthetic",
            params={},
            fetched_at=datetime.now(ZoneInfo("UTC")),
            json=_raw(sim_date, p.scale),
        )

    monkeypatch.setattr(simulate_mod, "fetch_weather", fake_fetch)
    return p


def _blocks(sim_date: date, mode: str) -> dict[int, GenerationBlock]:
    with session_scope() as db:
        rows = db.query(GenerationBlock).filter(
            GenerationBlock.plant_code == PLANT,
            GenerationBlock.sim_date == sim_date,
            GenerationBlock.data_mode == mode,
            GenerationBlock.is_current.is_(True),
        ).all()
        return {
            r.block_no: type("Row", (), {
                "total_mw": r.total_mw, "solar_mw": r.solar_mw, "wind_mw": r.wind_mw,
                "total_mwh": r.total_mwh, "data_label": r.data_label,
            })()
            for r in rows
        }


# --- Rule 1: Actuals are immutable ------------------------------------------
def test_live_actual_survives_a_weather_revision(provider):
    """Example 1: a published Actual stays put when the model revises its past."""
    today = datetime.now(IST).date()
    provider.scale = 1.0
    run_simulation_sync(PLANT, today, DataMode.LIVE, force_refetch=True)
    first = _blocks(today, "LIVE")

    x = current_block_no(TZ, today)
    actual_nos = [n for n in range(1, x + 1) if first[n].data_label == "LIVE_ESTIMATED"]
    assert actual_nos, "no completed blocks yet — test needs a non-midnight clock"

    # The provider now reports the SAME past intervals ~40% brighter.
    provider.scale = 1.4
    run_simulation_sync(PLANT, today, DataMode.LIVE, force_refetch=True)
    second = _blocks(today, "LIVE")

    for n in actual_nos:
        assert second[n].total_mw == pytest.approx(first[n].total_mw), (
            f"block {n} was a published Actual and moved "
            f"{first[n].total_mw} -> {second[n].total_mw}"
        )
        assert second[n].data_label == first[n].data_label


def test_live_forecast_blocks_still_move(provider):
    """The other half of the contract: not-yet-reached blocks are NOT frozen."""
    today = datetime.now(IST).date()
    provider.scale = 1.0
    run_simulation_sync(PLANT, today, DataMode.LIVE, force_refetch=True)
    first = _blocks(today, "LIVE")

    future_nos = [n for n, r in first.items() if r.data_label == "FORECAST_SIMULATED"]
    if not future_nos:
        pytest.skip("run happened in the last block of the day")

    provider.scale = 1.4
    run_simulation_sync(PLANT, today, DataMode.LIVE, force_refetch=True)
    second = _blocks(today, "LIVE")

    moved = [n for n in future_nos
             if abs(second[n].total_mw - first[n].total_mw) > 1e-6]
    assert moved, "forecast blocks must remain free to absorb new information"


def test_daily_summary_matches_the_blocks_it_is_made_of(provider):
    """A frozen block must not desync the day total from the published blocks."""
    from app.api.repository import get_summary

    today = datetime.now(IST).date()
    provider.scale = 1.0
    run_simulation_sync(PLANT, today, DataMode.LIVE, force_refetch=True)
    provider.scale = 1.4
    run_simulation_sync(PLANT, today, DataMode.LIVE, force_refetch=True)

    blocks = _blocks(today, "LIVE")
    with session_scope() as db:
        s = get_summary(db, PLANT, today, "LIVE")
        assert s is not None
        assert s.total_mwh == pytest.approx(
            sum(b.total_mwh for b in blocks.values()), rel=1e-6
        )


# --- Rule 2: Historical Actuals are immutable -------------------------------
def test_historical_actual_survives_a_refetch(provider):
    """Example 4: the archive revises, the stored Historical Actual does not."""
    day = datetime.now(IST).date() - timedelta(days=3)
    provider.scale = 1.0
    run_simulation_sync(PLANT, day, DataMode.HISTORICAL, force_refetch=True)
    first = _blocks(day, "HISTORICAL")
    assert all(r.data_label == "HISTORICAL_SIMULATED" for r in first.values())

    provider.scale = 1.4
    run_simulation_sync(PLANT, day, DataMode.HISTORICAL, force_refetch=True)
    second = _blocks(day, "HISTORICAL")

    for n, r in first.items():
        assert second[n].total_mw == pytest.approx(r.total_mw), f"block {n} moved"


def test_live_actual_is_republished_when_the_day_becomes_historical(provider):
    """One Actual per interval for its whole lifecycle.

    A day served live already has an Actual of record. When the daily job later
    writes the HISTORICAL series for that date, it must REPUBLISH those numbers,
    not recompute them from the archive — otherwise the value a consumer reads
    changes once, at the handover. Measured on real production data for
    2026-08-24 before this was enforced: 55 of 96 blocks differed, up to -24 MW on
    a single block and -3.13% on day energy.
    """
    day = datetime.now(IST).date() - timedelta(days=1)   # past date => all 96 reached
    provider.scale = 1.0
    run_simulation_sync(PLANT, day, DataMode.LIVE, force_refetch=True)
    live = _blocks(day, "LIVE")
    assert all(r.data_label == "LIVE_ESTIMATED" for r in live.values())

    # The archive disagrees materially with what was served live.
    provider.scale = 1.4
    run_simulation_sync(PLANT, day, DataMode.HISTORICAL, force_refetch=True)
    hist = _blocks(day, "HISTORICAL")

    for n in range(1, 97):
        assert hist[n].total_mw == pytest.approx(live[n].total_mw), (
            f"block {n} changed at the LIVE->HISTORICAL handover: "
            f"{live[n].total_mw} -> {hist[n].total_mw}"
        )
    # The label still names the series the consumer asked for.
    assert all(r.data_label == "HISTORICAL_SIMULATED" for r in hist.values())


def test_historical_backfill_still_uses_the_archive_when_no_live_exists(provider):
    """Inheritance must not break backfilling a day that was never served live."""
    day = datetime.now(IST).date() - timedelta(days=9)
    assert not _blocks(day, "LIVE"), "precondition: no LIVE coverage for this date"
    provider.scale = 1.3
    run_simulation_sync(PLANT, day, DataMode.HISTORICAL, force_refetch=True)
    hist = _blocks(day, "HISTORICAL")
    assert len(hist) == 96
    assert any(r.total_mw > 1.0 for r in hist.values()), "archive values not written"
    assert all(r.data_label == "HISTORICAL_SIMULATED" for r in hist.values())



def test_every_block_is_inherited_once_enforcement_is_on(provider):
    """With enforcement on, the whole day carries over — not just part of it.

    Historical is final regardless of the cutover, so there is no partial day: once
    a date was served live, /historical republishes all 96 of its blocks.
    """
    day = datetime.now(IST).date() - timedelta(days=6)
    provider.scale = 1.0
    run_simulation_sync(PLANT, day, DataMode.LIVE, force_refetch=True)
    live = _blocks(day, "LIVE")
    provider.scale = 1.7
    run_simulation_sync(PLANT, day, DataMode.HISTORICAL, force_refetch=True)
    hist = _blocks(day, "HISTORICAL")
    diverged = [n for n in live if abs(hist[n].total_mw - live[n].total_mw) > 1e-6]
    assert diverged == [], f"{len(diverged)} blocks changed at the handover"


def test_admin_reprocess_cannot_overwrite_a_published_actual(provider):
    """Admin reprocess is a DIAGNOSTIC. It must not replace what consumers see.

    It writes into its own version space with is_current=False, so the published
    rows are untouched and no read helper (all of which filter is_current) returns
    it — while the recomputed values are still stored for inspection.
    """
    from sqlalchemy import select as _select

    day = datetime.now(IST).date() - timedelta(days=4)
    provider.scale = 1.0
    run_simulation_sync(PLANT, day, DataMode.HISTORICAL, force_refetch=True)
    published = _blocks(day, "HISTORICAL")
    assert all(r.data_label == "HISTORICAL_SIMULATED" for r in published.values())

    provider.scale = 1.4
    run_simulation_sync(
        PLANT, day, DataMode.HISTORICAL, triggered_by="reprocess", force_refetch=True
    )

    # a) what consumers are served has not moved by a single MW
    after = _blocks(day, "HISTORICAL")
    for n, r in published.items():
        assert after[n].total_mw == pytest.approx(r.total_mw), f"block {n} was overwritten"
        assert after[n].data_label == "HISTORICAL_SIMULATED"

    # b) the diagnostic exists, is labelled, and is NOT current
    with session_scope() as db:
        diag = db.execute(_select(
            GenerationBlock.block_no, GenerationBlock.total_mw,
            GenerationBlock.data_label, GenerationBlock.is_current,
        ).where(
            GenerationBlock.plant_code == PLANT,
            GenerationBlock.sim_date == day,
            GenerationBlock.simulation_version == diagnostic_version("v1.0.0"),
        )).fetchall()
    assert len(diag) == 96, "diagnostic values were not stored"
    assert all(r.data_label == DIAGNOSTIC_BLOCK_LABEL for r in diag)
    assert all(r.is_current is False or r.is_current == 0 for r in diag)
    # and it really did compute something different
    dmap = {r.block_no: r.total_mw for r in diag}
    assert any(abs(dmap[n] - published[n].total_mw) > 1e-6 for n in published)


def test_reprocess_is_repeatable_and_never_leaks_into_reads(provider):
    """A diagnostic can be re-run freely; the published series stays put."""
    day = datetime.now(IST).date() - timedelta(days=5)
    provider.scale = 1.0
    run_simulation_sync(PLANT, day, DataMode.HISTORICAL, force_refetch=True)
    published = _blocks(day, "HISTORICAL")

    for scale in (1.2, 0.6, 1.9):
        provider.scale = scale
        run_simulation_sync(
            PLANT, day, DataMode.HISTORICAL, triggered_by="reprocess", force_refetch=True
        )

    after = _blocks(day, "HISTORICAL")
    assert len(after) == 96
    for n, r in published.items():
        assert after[n].total_mw == pytest.approx(r.total_mw)


def test_reported_totals_reflect_what_was_published(provider):
    """RunSummary must describe the published series, not the discarded one."""
    today = datetime.now(IST).date()
    provider.scale = 1.0
    run_simulation_sync(PLANT, today, DataMode.LIVE, force_refetch=True)
    provider.scale = 1.4
    summary = run_simulation_sync(PLANT, today, DataMode.LIVE, force_refetch=True)

    blocks = _blocks(today, "LIVE")
    assert summary.total_mwh == pytest.approx(
        sum(b.total_mwh for b in blocks.values()), rel=1e-6
    )


# --- Rules 3 & 4: Schedule revises forward only -----------------------------
def _seed_anchor(sim_date: date, mode: str, scale: float) -> None:
    """Write a full day of generation + weather to anchor a schedule on."""
    import math

    from app.db.models import WeatherBlock

    with session_scope() as db:
        for table in (GenerationBlock, WeatherBlock):
            db.query(table).filter(
                table.plant_code == PLANT,
                table.sim_date == sim_date,
                table.data_mode == mode,
            ).delete(synchronize_session=False)
        for n in range(1, 97):
            hour = (n - 1) * 0.25
            solar = (160.0 * scale * max(0.0, math.sin(math.pi * (hour - 6) / 12))
                     if 6 <= hour <= 18 else 0.0)
            wind = (40.0 + 25.0 * math.sin(2 * math.pi * hour / 24)) * scale
            start = datetime.combine(sim_date, time.min) + timedelta(minutes=15 * (n - 1))
            db.add(GenerationBlock(
                plant_code=PLANT, sim_date=sim_date, block_no=n,
                block_start=start, block_end=start + timedelta(minutes=15),
                solar_mw=solar, solar_mwh=solar * 0.25,
                wind_mw=wind, wind_mwh=wind * 0.25,
                total_mw=solar + wind, total_mwh=(solar + wind) * 0.25,
                solar_cuf=0.0, wind_cuf=0.0, hybrid_cuf=0.0,
                solar_status="OK", wind_status="OK",
                data_mode=mode, data_source="test", data_label=f"{mode}_SEED",
                data_quality_status="OK", simulation_version="v1.0.0",
                model_assumption_version="v1.0.0", plant_config_version=1,
                weather_source="test", weather_fetch_time=datetime.now(ZoneInfo("UTC")),
                is_current=True,
            ))
            db.add(WeatherBlock(
                plant_code=PLANT, sim_date=sim_date, block_no=n,
                block_start=start, block_end=start + timedelta(minutes=15),
                data_mode=mode, weather_source="test",
                fetched_at=datetime.now(ZoneInfo("UTC")), interpolated=False,
                cloud_cover=20.0, wind_speed_10m=6.0, wind_speed_100m=8.0 * scale,
            ))



def test_schedule_revision_respects_the_two_hour_lock(only_this_plant):
    """Tests 1-4: X..X+8 frozen, X+9 onward revisable, via the automatic job."""
    today = datetime.now(IST).date()
    x = current_block_no(TZ, today)
    lock = schedule_lock_blocks(settings_mod.get_settings())
    assert lock == 8, f"expected a 2h/8-block lock, got {lock}"
    if x + lock >= 96:
        pytest.skip("too late in the day for a revisable block to exist")

    _seed_anchor(today, "LIVE", 1.0)
    ensure_schedule(PLANT, today, force=True)
    with session_scope() as db:
        before = {r.block_no: r.total_p90_mw for r in get_schedule(db, PLANT, today)}
        issued_before = {r.block_no: r.issued_at for r in get_schedule(db, PLANT, today)}

    # A materially different forecast arrives.
    _seed_anchor(today, "LIVE", 1.5)
    only_this_plant.run_schedule_retry()
    with session_scope() as db:
        after = {r.block_no: r.total_p90_mw for r in get_schedule(db, PLANT, today)}
        issued_after = {r.block_no: r.issued_at for r in get_schedule(db, PLANT, today)}

    # Test 1 + 2: current block and the whole two-hour window are untouched.
    for k in range(0, lock + 1):
        n = x + k
        assert after[n] == pytest.approx(before[n]), (
            f"X+{k} (block {n}) is inside the {lock * 15}-min lock and must not move"
        )
        assert issued_after[n] == issued_before[n], f"X+{k} lost its issue time"
    # ...and everything before X too.
    for n in range(1, x):
        assert after[n] == pytest.approx(before[n]), f"past block {n} moved"

    # Test 3: the first block outside the window genuinely moved.
    first = x + lock + 1
    assert abs(after[first] - before[first]) > 1e-6, (
        f"X+{lock + 1} (block {first}) is the first revisable block and did not move"
    )

    # Test 4: every later block is revisable too.
    later = [n for n in range(first, 97) if abs(after[n] - before[n]) > 1e-6]
    assert len(later) == 96 - first + 1, (
        f"expected all of {first}..96 revisable, only {len(later)} moved"
    )


def test_lock_boundary_lands_on_the_documented_wall_clock():
    """Operating in 15:45-16:00: through 17:45-18:00 held, 18:00-18:15 first to move.

    Asserted against real timestamps, not index arithmetic, so an off-by-one in the
    block convention cannot pass.
    """
    day = date(2026, 8, 25)

    def window(n: int) -> str:
        s0 = datetime.combine(day, time.min) + timedelta(minutes=15 * (n - 1))
        return f"{s0:%H:%M}-{s0 + timedelta(minutes=15):%H:%M}"

    x = 15 * 4 + 45 // 15 + 1            # block containing 15:45
    assert window(x) == "15:45-16:00"
    assert window(x + 8) == "17:45-18:00"     # last protected
    assert window(x + 9) == "18:00-18:15"     # first revisable


def test_schedule_for_a_past_date_is_fully_protected():
    """Rule 3: nothing whose operational time has passed may be rewritten."""
    day = datetime.now(IST).date() - timedelta(days=2)
    _seed_anchor(day, "HISTORICAL", 1.0)
    ensure_schedule(PLANT, day, force=True)
    with session_scope() as db:
        before = {r.block_no: r.total_p90_mw for r in get_schedule(db, PLANT, day)}

    _seed_anchor(day, "HISTORICAL", 1.6)
    res = ensure_schedule(PLANT, day, force=True)
    assert res["blocks_protected"] == 96
    assert res["blocks_revised"] == 0

    with session_scope() as db:
        after = {r.block_no: r.total_p90_mw for r in get_schedule(db, PLANT, day)}
    assert after == pytest.approx(before)


def test_schedule_for_a_future_date_is_fully_revisable():
    """A day-ahead schedule has no operational past, so all 96 may move."""
    day = datetime.now(IST).date() + timedelta(days=1)
    _seed_anchor(day, "FORECAST", 1.0)
    ensure_schedule(PLANT, day, force=True)
    _seed_anchor(day, "FORECAST", 1.5)
    res = ensure_schedule(PLANT, day, force=True)
    assert res["blocks_protected"] == 0
    assert res["blocks_revised"] == 96


def test_unforced_schedule_stays_frozen():
    """Existing behaviour preserved: the automatic jobs never revise a schedule."""
    day = datetime.now(IST).date() + timedelta(days=1)
    _seed_anchor(day, "FORECAST", 1.0)
    ensure_schedule(PLANT, day, force=True)
    assert ensure_schedule(PLANT, day)["reason"] == "frozen"


# --- block/interval semantics ------------------------------------------------
def test_block_interval_convention_is_start_inclusive_end_exclusive():
    """13:50 is inside block 56 (13:45-14:00); 14:00 starts block 57."""
    assert current_block_no.__doc__  # documented convention
    day = date(2026, 8, 25)
    starts = {
        n: datetime.combine(day, time.min) + timedelta(minutes=15 * (n - 1))
        for n in range(1, 97)
    }
    assert starts[56].strftime("%H:%M") == "13:45"
    assert starts[57].strftime("%H:%M") == "14:00"
    assert starts[60].strftime("%H:%M") == "14:45"
    assert starts[61].strftime("%H:%M") == "15:00"


# --- cutover ----------------------------------------------------------------
class _Cut:
    """Settings stub carrying just the fields the invariant helpers read."""

    def __init__(self, raw: str, lock_minutes: int = 120):
        self.GENERATION_IMMUTABILITY_CUTOVER = raw
        self.SCHEDULE_REVISION_LOCK_MINUTES = lock_minutes


CUTOVER = "2026-08-25T15:00:00+05:30"


def test_cutover_controls_when_enforcement_starts():
    """The cutover is a switch-on instant, not a per-block filter.

    Before 2026-08-25 15:00 IST the old behaviour stands; from that instant onward
    enforcement is unconditional and stays on. Nothing stored is rewritten by the
    transition — the system simply stops rewriting from then on, which is what makes
    the change forward-only.
    """
    s = _Cut(CUTOVER)
    before = datetime(2026, 8, 25, 14, 59, 59, tzinfo=IST)
    at = datetime(2026, 8, 25, 15, 0, 0, tzinfo=IST)
    after = datetime(2026, 8, 25, 15, 0, 1, tzinfo=IST)
    assert is_enforced(s, now=before) is False
    assert is_enforced(s, now=at) is True          # inclusive at the instant
    assert is_enforced(s, now=after) is True
    assert is_enforced(s, now=datetime(2027, 1, 1, tzinfo=IST)) is True


def test_cutover_is_interpreted_in_ist_not_utc():
    """15:00 IST is 09:30 UTC. Read as UTC it would switch on 5.5h too early."""
    s = _Cut(CUTOVER)
    parsed = _parse_cutover(CUTOVER)
    assert parsed.astimezone(ZoneInfo("UTC")).strftime("%H:%M") == "09:30"
    # 09:30 UTC == 15:00 IST: enforced. 09:29 UTC is not.
    assert is_enforced(s, now=datetime(2026, 8, 25, 9, 29, tzinfo=ZoneInfo("UTC"))) is False
    assert is_enforced(s, now=datetime(2026, 8, 25, 9, 30, tzinfo=ZoneInfo("UTC"))) is True


def test_missing_or_malformed_cutover_fails_safe_toward_immutability():
    """A typo must not silently switch a data-integrity invariant off."""
    for raw in ("", "   ", "not-a-timestamp", "2026-08-25T15:00:00"):
        assert _parse_cutover(raw) is None
        assert is_enforced(_Cut(raw)) is True


# --- label / is_final contract ----------------------------------------------
def test_historical_actual_is_final_regardless_of_cutover():
    """A completed day is history; history does not get a second opinion.

    Even with the cutover still in the future, a HISTORICAL actual reports final.
    """
    future_cutover = _Cut("2099-01-01T00:00:00+05:30")
    assert is_enforced(future_cutover) is False
    assert is_final_actual("HISTORICAL_SIMULATED", future_cutover) is True
    # A LIVE actual only becomes final once enforcement starts.
    assert is_final_actual("LIVE_ESTIMATED", future_cutover) is False


def test_only_actual_labels_are_ever_final():
    s = _Cut("")
    for label in ACTUAL_BLOCK_LABELS:
        assert is_final_actual(label, s) is True
    # REPROCESSED is a diagnostic, never a published Actual.
    for label in ("FORECAST_SIMULATED", "REPROCESSED", "FAILED", "PARTIAL", None):
        assert is_final_actual(label, s) is False



def test_protected_prefix_covers_the_current_block_plus_the_lock():
    s = _Cut("")
    today = datetime.now(IST).date()
    nos = list(range(1, 97))
    x = current_block_no(TZ, today)
    lock = schedule_lock_blocks(settings_mod.get_settings())
    expected_upto = min(96, x + lock)

    protected = protected_schedule_block_nos(today, nos, TZ, s)
    assert protected == set(range(1, expected_upto + 1))
    if expected_upto < 96:
        assert (expected_upto + 1) not in protected
        assert first_revisable_schedule_block_no(today, TZ, s) == expected_upto + 1
    else:
        assert first_revisable_schedule_block_no(today, TZ, s) is None


def test_past_schedule_is_protected_even_before_the_cutover():
    """Historical Schedule is final regardless of the cutover."""
    future_cutover = _Cut("2099-01-01T00:00:00+05:30")
    past = datetime.now(IST).date() - timedelta(days=3)
    nos = list(range(1, 97))
    assert protected_schedule_block_nos(past, nos, TZ, future_cutover) == set(nos)
    # A future date stays fully revisable.
    ahead = datetime.now(IST).date() + timedelta(days=1)
    assert protected_schedule_block_nos(ahead, nos, TZ, _Cut("")) == set()


# --- error / concurrency paths ----------------------------------------------
def test_failed_refresh_leaves_published_actuals_intact(provider, monkeypatch):
    """A provider failure must not blank or move an already-published Actual."""
    today = datetime.now(IST).date()
    provider.scale = 1.0
    run_simulation_sync(PLANT, today, DataMode.LIVE, force_refetch=True)
    before = _blocks(today, "LIVE")

    async def boom(*a, **k):
        raise wx_client.WeatherFetchError("provider down", 429)

    monkeypatch.setattr(simulate_mod, "fetch_weather", boom)
    # Falls back to stored weather rather than failing the day (existing behaviour).
    run_simulation_sync(PLANT, today, DataMode.LIVE, force_refetch=True)
    after = _blocks(today, "LIVE")

    x = current_block_no(TZ, today)
    for n in range(1, x + 1):
        if before[n].data_label in ACTUAL_BLOCK_LABELS:
            assert after[n].total_mw == pytest.approx(before[n].total_mw)


def test_repeated_refreshes_converge_on_one_actual(provider):
    """Idempotence: many refreshes, one Actual — and exactly 96 current rows."""
    today = datetime.now(IST).date()
    provider.scale = 1.0
    run_simulation_sync(PLANT, today, DataMode.LIVE, force_refetch=True)
    baseline = _blocks(today, "LIVE")
    x = current_block_no(TZ, today)

    for scale in (1.2, 0.7, 1.5, 0.9):
        provider.scale = scale
        run_simulation_sync(PLANT, today, DataMode.LIVE, force_refetch=True)

    final = _blocks(today, "LIVE")
    assert len(final) == 96
    for n in range(1, x + 1):
        if baseline[n].data_label == "LIVE_ESTIMATED":
            assert final[n].total_mw == pytest.approx(baseline[n].total_mw)


@pytest.fixture
def with_cutover(monkeypatch):
    """Override the cutover on the live Settings instance for one test.

    `get_settings()` is lru_cached, so the cached instance is what every code path
    reads; patching the attribute on it is the narrowest way to exercise a real
    cutover instant end-to-end.
    """
    def _set(value: str):
        monkeypatch.setattr(
            settings_mod.get_settings(), "GENERATION_IMMUTABILITY_CUTOVER", value,
            raising=False,
        )
    return _set



def test_runtime_before_enforcement_actuals_still_move(provider, with_cutover):
    """With the cutover still ahead, the OLD behaviour stands — nothing is frozen.

    This is what makes the change safe to deploy early: until the configured instant
    arrives, behaviour is byte-for-byte what it was.
    """
    with_cutover("2099-01-01T00:00:00+05:30")
    day = datetime.now(IST).date() - timedelta(days=7)

    provider.scale = 1.0
    run_simulation_sync(PLANT, day, DataMode.LIVE, force_refetch=True)
    first = _blocks(day, "LIVE")
    provider.scale = 1.5
    run_simulation_sync(PLANT, day, DataMode.LIVE, force_refetch=True)
    second = _blocks(day, "LIVE")

    daylight = [n for n in first if first[n].total_mw > 1.0]
    assert daylight, "precondition: some generating blocks"
    assert any(abs(second[n].total_mw - first[n].total_mw) > 1e-6 for n in daylight), (
        "before the cutover, LIVE actuals must keep the old rewrite behaviour"
    )


def test_runtime_after_enforcement_every_published_actual_freezes(provider, with_cutover):
    """Once enforcement is on it is unconditional — including blocks written earlier."""
    with_cutover("2020-01-01T00:00:00+05:30")   # long past => enforced
    day = datetime.now(IST).date() - timedelta(days=8)

    provider.scale = 1.0
    run_simulation_sync(PLANT, day, DataMode.LIVE, force_refetch=True)
    first = _blocks(day, "LIVE")
    provider.scale = 1.5
    run_simulation_sync(PLANT, day, DataMode.LIVE, force_refetch=True)
    second = _blocks(day, "LIVE")

    for n in first:
        assert second[n].total_mw == pytest.approx(first[n].total_mw), f"block {n} moved"



def test_is_final_tracks_the_enforcement_switch(with_cutover):
    """`is_final` must not claim finality before enforcement begins."""
    with_cutover("2099-01-01T00:00:00+05:30")
    s = settings_mod.get_settings()
    assert is_enforced(s) is False
    assert is_final_actual("LIVE_ESTIMATED", s) is False
    # ...but a completed day is final regardless.
    assert is_final_actual("HISTORICAL_SIMULATED", s) is True

    with_cutover("2020-01-01T00:00:00+05:30")
    s = settings_mod.get_settings()
    assert is_enforced(s) is True
    assert is_final_actual("LIVE_ESTIMATED", s) is True


def test_concurrent_refreshes_cannot_move_a_published_actual(provider):
    """Two refreshes racing on the same plant/date must not restate an Actual.

    Whoever wins, both writers read the same stored Actual and carry the same value
    forward, so the frozen value survives regardless of interleaving. Either thread
    is allowed to fail outright (the unique constraint on
    plant/date/block/mode/version is what makes a double-insert impossible) — what
    must never happen is a *changed* Actual.
    """
    import threading

    today = datetime.now(IST).date()
    provider.scale = 1.0
    run_simulation_sync(PLANT, today, DataMode.LIVE, force_refetch=True)
    before = _blocks(today, "LIVE")
    x = current_block_no(TZ, today)
    frozen_nos = [
        n for n in range(1, x + 1) if before[n].data_label in ACTUAL_BLOCK_LABELS
    ]

    errors: list[Exception] = []

    def refresh(scale: float) -> None:
        try:
            provider.scale = scale
            run_simulation_sync(PLANT, today, DataMode.LIVE, force_refetch=True)
        except Exception as exc:  # noqa: BLE001 — a losing writer may legitimately fail
            errors.append(exc)

    threads = [threading.Thread(target=refresh, args=(s,)) for s in (1.6, 0.5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    after = _blocks(today, "LIVE")
    assert len(after) == 96, f"table left inconsistent; errors={errors}"
    for n in frozen_nos:
        assert after[n].total_mw == pytest.approx(before[n].total_mw), (
            f"block {n} moved under concurrent refresh (errors={errors})"
        )


def test_schedule_revision_keeps_exactly_96_current_blocks():
    today = datetime.now(IST).date()
    _seed_anchor(today, "LIVE", 1.0)
    ensure_schedule(PLANT, today, force=True)
    _seed_anchor(today, "LIVE", 1.3)
    ensure_schedule(PLANT, today, force=True)
    with session_scope() as db:
        rows = get_schedule(db, PLANT, today)
        assert len(rows) == 96
        assert len({r.block_no for r in rows}) == 96
        stale = db.query(ScheduleBlock).filter(
            ScheduleBlock.plant_code == PLANT,
            ScheduleBlock.sim_date == today,
            ScheduleBlock.is_current.is_(False),
        ).count()
        assert stale >= 0  # history is demoted, never required to vanish


# --- automatic future-only schedule revision --------------------------------
@pytest.fixture
def only_this_plant(monkeypatch):
    """Point the scheduler job at this module's plant only.

    `run_schedule_retry` iterates every active plant. Without this it would touch
    HYBRID01, which other test modules own in the shared process-global database.
    """
    import app.scheduler.service as svc

    monkeypatch.setattr(svc, "_active_plants", lambda: [(PLANT, TZ)])
    return svc


def test_hourly_job_revises_future_schedule_automatically(only_this_plant):
    """The requirement: no human runs --force; the hourly pass does it.

    Reuses the existing schedule-maintenance job rather than adding a second
    scheduling architecture, and it must move X+1..96 only.
    """
    today = datetime.now(IST).date()
    x = current_block_no(TZ, today)
    if x >= 96:
        pytest.skip("no future blocks left today")

    _seed_anchor(today, "LIVE", 1.0)
    ensure_schedule(PLANT, today, force=True)
    with session_scope() as db:
        before = {r.block_no: r.total_p90_mw for r in get_schedule(db, PLANT, today)}

    # Newer forecast information lands in the anchor the schedule is built from.
    _seed_anchor(today, "LIVE", 1.5)
    only_this_plant.run_schedule_retry()          # <- the automatic path

    with session_scope() as db:
        after = {r.block_no: r.total_p90_mw for r in get_schedule(db, PLANT, today)}

    moved_past = [n for n in range(1, x + 1) if abs(after[n] - before[n]) > 1e-9]
    moved_future = [n for n in range(x + 1, 97) if abs(after[n] - before[n]) > 1e-9]
    assert moved_past == [], f"current/past schedule blocks moved: {moved_past}"
    assert moved_future, "the hourly job did not revise any future block"


def test_hourly_job_still_issues_a_missing_schedule(only_this_plant):
    """Original self-heal behaviour must survive the broadened job."""
    from app.schedule import has_schedule

    today = datetime.now(IST).date()
    ahead = today + timedelta(days=1)
    with session_scope() as db:
        db.query(ScheduleBlock).filter(
            ScheduleBlock.plant_code == PLANT, ScheduleBlock.sim_date == ahead,
        ).delete(synchronize_session=False)
    _seed_anchor(today, "LIVE", 1.0)
    _seed_anchor(ahead, "FORECAST", 1.0)
    with session_scope() as db:
        assert not has_schedule(db, PLANT, ahead)

    only_this_plant.run_schedule_retry()

    with session_scope() as db:
        assert has_schedule(db, PLANT, ahead), "missing schedule was not issued"


def test_hourly_revision_can_be_switched_off(only_this_plant, monkeypatch):
    """SCHEDULE_INTRADAY_REVISION_ENABLED=false restores whole-day freeze."""
    today = datetime.now(IST).date()
    if current_block_no(TZ, today) >= 96:
        pytest.skip("no future blocks left today")
    _seed_anchor(today, "LIVE", 1.0)
    ensure_schedule(PLANT, today, force=True)
    with session_scope() as db:
        before = {r.block_no: r.total_p90_mw for r in get_schedule(db, PLANT, today)}

    monkeypatch.setattr(
        settings_mod.get_settings(), "SCHEDULE_INTRADAY_REVISION_ENABLED", False,
        raising=False,
    )
    _seed_anchor(today, "LIVE", 1.5)
    only_this_plant.run_schedule_retry()

    with session_scope() as db:
        after = {r.block_no: r.total_p90_mw for r in get_schedule(db, PLANT, today)}
    assert after == pytest.approx(before), "revision ran while disabled"


def test_repeated_hourly_revisions_never_disturb_the_protected_prefix(only_this_plant):
    """Running the job many times must not erode the frozen part of the day."""
    today = datetime.now(IST).date()
    x = current_block_no(TZ, today)
    if x >= 96:
        pytest.skip("no future blocks left today")
    _seed_anchor(today, "LIVE", 1.0)
    ensure_schedule(PLANT, today, force=True)
    with session_scope() as db:
        baseline = {r.block_no: r.total_p90_mw for r in get_schedule(db, PLANT, today)}

    for scale in (1.3, 0.7, 1.6, 0.9):
        _seed_anchor(today, "LIVE", scale)
        only_this_plant.run_schedule_retry()

    with session_scope() as db:
        # Build the plain dict INSIDE the session: the ORM rows expire on close.
        final = {r.block_no: r.total_p90_mw for r in get_schedule(db, PLANT, today)}
    assert len(final) == 96
    for n in range(1, x + 1):
        assert final[n] == pytest.approx(baseline[n]), f"protected block {n} moved"


def test_schedule_revision_makes_no_provider_call(only_this_plant, monkeypatch):
    """A revision must never spend the Open-Meteo quota the prefetch depends on."""
    calls = []

    async def tripwire(*a, **k):
        calls.append(1)
        raise AssertionError("ensure_schedule must not fetch weather")

    monkeypatch.setattr(simulate_mod, "fetch_weather", tripwire)
    today = datetime.now(IST).date()
    _seed_anchor(today, "LIVE", 1.0)
    ensure_schedule(PLANT, today, force=True)
    _seed_anchor(today, "LIVE", 1.4)
    only_this_plant.run_schedule_retry()
    assert calls == [], "the hourly schedule pass called the weather provider"


# --- two-hour schedule lock: edges and configuration ------------------------
def test_lock_window_never_freezes_the_next_days_schedule():
    """The trap: current_block_no returns 0 for a future date.

    A naive `x + lock` would protect blocks 1..8 of tomorrow and silently freeze the
    first two hours of every day-ahead schedule. Each date owns its own schedule, so
    the lock must not spill across midnight.
    """
    s = _Cut("")
    nos = list(range(1, 97))
    today = datetime.now(IST).date()
    for ahead in (1, 2, 7):
        d = today + timedelta(days=ahead)
        assert current_block_no(TZ, d) == 0, "precondition: future date has no current block"
        assert protected_schedule_block_nos(d, nos, TZ, s) == set(), (
            f"today+{ahead} must be fully revisable — its schedule is independent"
        )
        assert first_revisable_schedule_block_no(d, TZ, s) == 1


def test_lock_window_does_not_reach_back_into_history():
    """A finished day stays fully frozen; the lock changes nothing there."""
    s = _Cut("")
    nos = list(range(1, 97))
    past = datetime.now(IST).date() - timedelta(days=1)
    assert protected_schedule_block_nos(past, nos, TZ, s) == set(nos)
    assert first_revisable_schedule_block_no(past, TZ, s) is None


@pytest.fixture
def at_block(monkeypatch):
    """Pin `current_block_no` as the boundary helpers see it, to test edge cases."""
    import app.immutability as immut

    def _set(n: int):
        monkeypatch.setattr(immut, "current_block_no", lambda tz, sim_date: n)
    return _set


def test_end_of_day_never_overflows_the_96_block_day(at_block):
    """Test 6: near midnight the lock clamps; no index past 96, zero revision is fine."""
    s = _Cut("")
    nos = list(range(1, 97))
    today = datetime.now(IST).date()
    lock = schedule_lock_blocks(settings_mod.get_settings())

    for x, expect_upto in [
        (87, 95),   # 87+8 = 95 -> block 96 still revisable
        (88, 96),   # 88+8 = 96 -> exactly the last block, nothing revisable
        (94, 96),   # would be 102 -> clamped
        (96, 96),   # last block of the day
    ]:
        at_block(x)
        protected = protected_schedule_block_nos(today, nos, TZ, s)
        assert max(protected) <= 96, f"X={x} produced an index beyond the day"
        assert protected == set(range(1, expect_upto + 1)), f"X={x}"
        first = first_revisable_schedule_block_no(today, TZ, s)
        if expect_upto >= 96:
            assert first is None, f"X={x} should leave nothing revisable, got {first}"
        else:
            assert first == expect_upto + 1
    assert lock == 8


def test_end_of_day_revision_is_a_no_op_not_an_error(at_block, only_this_plant):
    """A fully committed day must skip cleanly, not raise or rewrite."""
    today = datetime.now(IST).date()
    _seed_anchor(today, "LIVE", 1.0)
    ensure_schedule(PLANT, today, force=True)
    with session_scope() as db:
        before = {r.block_no: r.total_p90_mw for r in get_schedule(db, PLANT, today)}

    at_block(96)                       # the whole day is inside the lock
    _seed_anchor(today, "LIVE", 1.8)
    only_this_plant.run_schedule_retry()   # must not raise

    with session_scope() as db:
        after = {r.block_no: r.total_p90_mw for r in get_schedule(db, PLANT, today)}
    assert after == pytest.approx(before), "a fully committed day was rewritten"


def test_manual_force_obeys_the_same_lock(only_this_plant):
    """Test 8: --force means 'attempt now', never 'override committed blocks'."""
    today = datetime.now(IST).date()
    x = current_block_no(TZ, today)
    lock = schedule_lock_blocks(settings_mod.get_settings())
    if x + lock >= 96:
        pytest.skip("too late in the day for a revisable block to exist")

    _seed_anchor(today, "LIVE", 1.0)
    ensure_schedule(PLANT, today, force=True)
    with session_scope() as db:
        before = {r.block_no: r.total_p90_mw for r in get_schedule(db, PLANT, today)}

    _seed_anchor(today, "LIVE", 1.6)
    res = ensure_schedule(PLANT, today, force=True)      # the manual path
    with session_scope() as db:
        after = {r.block_no: r.total_p90_mw for r in get_schedule(db, PLANT, today)}

    assert res["blocks_protected"] == min(96, x + lock)
    for k in range(0, lock + 1):
        assert after[x + k] == pytest.approx(before[x + k]), f"force moved X+{k}"
    assert abs(after[x + lock + 1] - before[x + lock + 1]) > 1e-6


def test_hourly_job_and_manual_force_agree_on_the_boundary(only_this_plant):
    """Test 7: both paths must land on the identical protected count."""
    today = datetime.now(IST).date()
    x = current_block_no(TZ, today)
    lock = schedule_lock_blocks(settings_mod.get_settings())
    if x + lock >= 96:
        pytest.skip("too late in the day")

    _seed_anchor(today, "LIVE", 1.0)
    manual = ensure_schedule(PLANT, today, force=True)
    _seed_anchor(today, "LIVE", 1.4)
    only_this_plant.run_schedule_retry()
    _seed_anchor(today, "LIVE", 1.7)
    manual2 = ensure_schedule(PLANT, today, force=True)

    assert manual2["blocks_protected"] == manual["blocks_protected"] == min(96, x + lock)
    assert manual2["blocks_revised"] == 96 - min(96, x + lock)


def test_lock_minutes_is_configuration_driven(monkeypatch):
    """0 restores the old X-only boundary; the value is read from Settings only."""
    s = settings_mod.get_settings()
    nos = list(range(1, 97))
    today = datetime.now(IST).date()
    x = current_block_no(TZ, today)

    monkeypatch.setattr(s, "SCHEDULE_REVISION_LOCK_MINUTES", 0, raising=False)
    assert schedule_lock_blocks(s) == 0
    assert protected_schedule_block_nos(today, nos, TZ, s) == set(range(1, x + 1))

    monkeypatch.setattr(s, "SCHEDULE_REVISION_LOCK_MINUTES", 60, raising=False)
    assert schedule_lock_blocks(s) == 4
    assert protected_schedule_block_nos(today, nos, TZ, s) == set(range(1, min(96, x + 4) + 1))


def test_lock_minutes_rounds_up_and_ignores_negatives(monkeypatch):
    """A non-multiple of the grid must never protect LESS time than configured."""
    s = settings_mod.get_settings()
    monkeypatch.setattr(s, "SCHEDULE_REVISION_LOCK_MINUTES", 100, raising=False)
    assert schedule_lock_blocks(s) == 7        # 7*15 = 105 >= 100
    monkeypatch.setattr(s, "SCHEDULE_REVISION_LOCK_MINUTES", 16, raising=False)
    assert schedule_lock_blocks(s) == 2        # 2*15 = 30 >= 16
    monkeypatch.setattr(s, "SCHEDULE_REVISION_LOCK_MINUTES", -50, raising=False)
    assert schedule_lock_blocks(s) == 0        # clamped, never negative


def test_historical_schedule_untouched_by_automatic_revision(only_this_plant):
    """Test 5: the hourly pass must never recompute a finished day's schedule."""
    past = datetime.now(IST).date() - timedelta(days=2)
    _seed_anchor(past, "HISTORICAL", 1.0)
    ensure_schedule(PLANT, past, force=True)
    with session_scope() as db:
        before = {r.block_no: r.total_p90_mw for r in get_schedule(db, PLANT, past)}

    _seed_anchor(past, "HISTORICAL", 1.9)
    today = datetime.now(IST).date()
    _seed_anchor(today, "LIVE", 1.5)
    only_this_plant.run_schedule_retry()

    with session_scope() as db:
        after = {r.block_no: r.total_p90_mw for r in get_schedule(db, PLANT, past)}
    assert after == pytest.approx(before), "historical schedule was recalculated"
