"""Forward-dated plant-config versions.

The invariant under test: a config version dated in the future must change nothing
about today or any past date — including when those dates are re-simulated — while
applying cleanly from its effective date onward.

Like `test_immutability`, this module works on a plant of its own. `db_base._engine`
is a process-global singleton, so a full-suite run shares ONE database; future-dating
the shared HYBRID01 config here would change what other modules find active.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

_TMPDIR = tempfile.mkdtemp(prefix="rensim_effdate_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDIR}/test.db".replace("\\", "/")
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["ADMIN_BOOTSTRAP_KEY"] = "test-admin-key"
os.environ["PLANT_CODE"] = "HYBRID01"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.config.settings as settings_mod  # noqa: E402
import app.db.base as db_base  # noqa: E402

settings_mod.get_settings.cache_clear()
db_base._engine = None
db_base._SessionLocal = None

from app.db.base import session_scope  # noqa: E402
from app.db.models import PlantConfig  # noqa: E402
from app.db.seed import run_seed  # noqa: E402
from app.main import app  # noqa: E402
from app.services import create_config_version  # noqa: E402
from app.simulate import load_active_config, load_config_for_date  # noqa: E402

ADMIN = {"X-API-Key": "test-admin-key"}
PLANT = "EFFDATE01"
TZ = "Asia/Kolkata"
CUTOVER = date(2026, 9, 17)


@pytest.fixture(scope="module")
def client():
    """Seed the baseline plant, then clone its config under this module's plant code."""
    from sqlalchemy import inspect, select

    from app.db.models import Plant

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
                plant_code=PLANT, plant_name="Effective-Date Test Plant",
                active_config_version=1,
            )
            db.add(plant)
            db.flush()
            skip = {
                "id", "plant_id", "plant_code", "plant_name", "created_at",
                "effective_from_date",
            }
            fields = {
                c.key: getattr(base, c.key)
                for c in inspect(PlantConfig).mapper.column_attrs
                if c.key not in skip
            }
            fields["config_version"] = 1
            fields["timezone"] = TZ
            fields["wind_loss_factor"] = 0.08
            db.add(PlantConfig(
                plant_id=plant.id, plant_code=PLANT,
                plant_name="Effective-Date Test Plant", **fields,
            ))
    with TestClient(app) as c:
        yield c


def _active_versions(db) -> list[int]:
    return sorted(
        c.config_version
        for c in db.query(PlantConfig).filter(
            PlantConfig.plant_code == PLANT, PlantConfig.is_active.is_(True)
        )
    )


def test_no_effective_date_behaves_exactly_as_before(client):
    """With no dates set anywhere, date-gated lookup == the old lookup."""
    with session_scope() as db:
        for d in (date(2020, 1, 1), date(2026, 9, 16), date(2030, 1, 1)):
            assert (
                load_config_for_date(db, PLANT, d).config_version
                == load_active_config(db, PLANT).config_version
            )


def test_forward_dated_version_does_not_touch_earlier_dates(client):
    with session_scope() as db:
        base = load_active_config(db, PLANT).config_version
        new_ver = create_config_version(
            db, PLANT, {"wind_loss_factor": 0.0, "effective_from_date": CUTOVER}
        ).config_version
    assert new_ver == base + 1

    with session_scope() as db:
        # Every date before the cutover keeps the old version and its parameters.
        for d in (CUTOVER - timedelta(days=1), date(2024, 1, 1)):
            cfg = load_config_for_date(db, PLANT, d)
            assert cfg.config_version == base
            assert cfg.wind_loss_factor == 0.08
        # From the cutover onward, the new one.
        for d in (CUTOVER, CUTOVER + timedelta(days=30)):
            cfg = load_config_for_date(db, PLANT, d)
            assert cfg.config_version == new_ver
            assert cfg.wind_loss_factor == 0.0


def test_forward_dated_version_keeps_the_previous_one_active(client):
    """Retiring it would leave earlier dates with no config at all."""
    with session_scope() as db:
        assert len(_active_versions(db)) >= 2


def test_effective_date_is_not_inherited_by_the_next_version(client):
    with session_scope() as db:
        cfg = create_config_version(db, PLANT, {"plant_name": "Renamed Plant"})
        assert cfg.effective_from_date is None
        ver = cfg.config_version
    with session_scope() as db:
        # An undated version supersedes everything, for every date.
        assert load_config_for_date(db, PLANT, date(2020, 1, 1)).config_version == ver
        assert _active_versions(db) == [ver]


def test_put_config_round_trips_the_effective_date(client):
    r = client.put(
        f"/plants/{PLANT}/config",
        headers=ADMIN,
        json={"wind_loss_factor": 0.05, "effective_from_date": "2027-01-01"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["effective_from_date"] == "2027-01-01"
    with session_scope() as db:
        assert load_config_for_date(db, PLANT, date(2026, 12, 31)).wind_loss_factor != 0.05
        assert load_config_for_date(db, PLANT, date(2027, 1, 1)).wind_loss_factor == 0.05


def test_refresh_forecast_refuses_today_and_the_past(client):
    today = datetime.now(ZoneInfo(TZ)).date()
    for d in (today, today - timedelta(days=1)):
        r = client.post(
            "/admin/refresh-forecast",
            headers=ADMIN,
            json={"plant_code": PLANT, "dates": [d.isoformat()]},
        )
        assert r.status_code == 400, r.text
        assert "Future dates only" in r.json()["detail"]


def test_refresh_forecast_requires_admin(client):
    r = client.post(
        "/admin/refresh-forecast",
        json={"plant_code": PLANT, "dates": ["2030-01-01"]},
    )
    assert r.status_code in (401, 403), r.text


# --------------------------------------------------------------------------- #
# End-to-end: the actual rollout guarantee
# --------------------------------------------------------------------------- #
def _synthetic_raw(sim_date: date) -> dict:
    """A constant-wind day, so any change in wind MW comes from the config alone."""
    import math
    from datetime import time as _time

    times: list[str] = []
    t = datetime.combine(sim_date - timedelta(days=1), _time.min)
    last = datetime.combine(sim_date + timedelta(days=1), _time(23, 0))
    while t <= last:
        times.append(t.isoformat(timespec="minutes"))
        t += timedelta(hours=1)

    def solar(ts: str) -> float:
        hour = int(ts[11:13])
        if 6 <= hour <= 18:
            return 900.0 * max(0.0, math.sin(math.pi * (hour - 6) / 12))
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
            "wind_speed_10m": [4.0] * n,
            "wind_speed_100m": [6.0] * n,
            "wind_speed_120m": [6.1] * n,
            "wind_speed_180m": [6.4] * n,
            "wind_direction_100m": [180.0] * n,
            "wind_gusts_10m": [8.0] * n,
            "surface_pressure": [950.0] * n,
        },
    }


def test_forward_dated_config_changes_tomorrow_but_not_today(client, monkeypatch):
    """The rollout guarantee, end to end.

    Simulate today and tomorrow, forward-date a wind change to tomorrow, re-simulate
    BOTH, and assert today's wind is byte-identical while tomorrow's has moved.
    """
    import app.simulate as simulate_mod
    from app.db.models import GenerationBlock
    from app.simulate import run_simulation_sync
    from app.weather.client import DataMode, RawFetch

    async def fake_fetch(plant, sim_date, mode, settings=None, today=None):
        return RawFetch(
            plant_code=plant.plant_code, sim_date=sim_date, mode=mode,
            provider="open-meteo", weather_source="test:synthetic",
            request_url="test://synthetic", params={},
            fetched_at=datetime.now(ZoneInfo("UTC")), json=_synthetic_raw(sim_date),
        )

    monkeypatch.setattr(simulate_mod, "fetch_weather", fake_fetch)

    today = datetime.now(ZoneInfo(TZ)).date()
    tomorrow = today + timedelta(days=1)

    def wind_mwh(d: date, mode: str) -> float:
        with session_scope() as db:
            return sum(
                r.wind_mwh or 0.0
                for r in db.query(GenerationBlock).filter(
                    GenerationBlock.plant_code == PLANT,
                    GenerationBlock.sim_date == d,
                    GenerationBlock.data_mode == mode,
                    GenerationBlock.is_current.is_(True),
                )
            )

    run_simulation_sync(PLANT, today, DataMode.LIVE, force_refetch=True)
    run_simulation_sync(PLANT, tomorrow, DataMode.FORECAST, force_refetch=True)
    today_before, tomorrow_before = wind_mwh(today, "LIVE"), wind_mwh(tomorrow, "FORECAST")
    assert today_before > 0 and tomorrow_before > 0

    # A wind curve that roughly triples output at 6 m/s, effective TOMORROW.
    with session_scope() as db:
        create_config_version(db, PLANT, {
            "wind_power_curve": [[0.0, 0.0], [2.0, 0.0], [6.0, 73175.0],
                                 [8.5, 111228.0], [19.6, 111228.0], [19.7, 0.0]],
            "wind_loss_factor": 0.0,
            "air_density_correction": False,
            "cut_in_ms": 2.3,
            "effective_from_date": tomorrow,
        })

    run_simulation_sync(PLANT, today, DataMode.LIVE, force_refetch=True)
    run_simulation_sync(PLANT, tomorrow, DataMode.FORECAST, force_refetch=True)

    assert wind_mwh(today, "LIVE") == pytest.approx(today_before), (
        "today's wind moved — a forward-dated config leaked backwards"
    )
    assert wind_mwh(tomorrow, "FORECAST") > tomorrow_before * 2, (
        "tomorrow's wind did not pick up the new config"
    )


def test_dashboard_config_route_also_honours_the_effective_date(client):
    """The keyless console route writes config too, and is what an operator uses.

    Regression guard: the first production rollout went out through this route while
    only the keyed `/plants/{code}/config` route was covered, so a drop of
    `effective_from_date` here would not have failed any test.
    """
    with session_scope() as db:
        before = load_active_config(db, PLANT).config_version

    r = client.put(
        f"/dashboard/api/config/{PLANT}",
        json={"wind_loss_factor": 0.11, "effective_from_date": "2028-06-01"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["config_version"] == before + 1

    with session_scope() as db:
        new = load_active_config(db, PLANT)
        assert new.effective_from_date == date(2028, 6, 1), (
            f"dashboard route dropped the effective date: {new.effective_from_date!r}"
        )
        # Dated version must not retire the one earlier dates still need.
        assert before in _active_versions(db)
        assert load_config_for_date(db, PLANT, date(2028, 5, 31)).config_version == before
        assert load_config_for_date(db, PLANT, date(2028, 6, 1)).wind_loss_factor == 0.11


def test_get_config_reports_the_effective_date(client):
    """GET must show what the database stores.

    GET and PUT used to build PlantConfigOut from two separate copies of the same
    field list. PUT reported the effective date and GET reported null for the very
    same row, which is what made a correct production rollout look broken.
    """
    r = client.put(
        f"/plants/{PLANT}/config",
        headers=ADMIN,
        json={"wind_loss_factor": 0.07, "effective_from_date": "2029-03-04"},
    )
    assert r.status_code == 200, r.text
    put_body = r.json()

    g = client.get(f"/plants/{PLANT}/config", headers=ADMIN)
    assert g.status_code == 200, g.text
    assert g.json()["effective_from_date"] == "2029-03-04"
    # GET and PUT must describe the same row identically.
    assert g.json() == put_body
