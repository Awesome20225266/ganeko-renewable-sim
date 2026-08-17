"""Schedule endpoints: auth, freeze semantics, wrapper policy, and a regression
guard that the pre-existing API responses are byte-identical.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta

# Isolated test DB BEFORE importing app modules.
_TMPDIR = tempfile.mkdtemp(prefix="rensim_sched_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDIR}/test.db".replace("\\", "/")
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["ADMIN_BOOTSTRAP_KEY"] = "test-admin-key"
os.environ["PLANT_CODE"] = "HYBRID01"
os.environ["RENEWABLE_WRAPPER_USER_API_KEY"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.config.settings as settings_mod  # noqa: E402
import app.db.base as db_base  # noqa: E402

settings_mod.get_settings.cache_clear()
db_base._engine = None
db_base._SessionLocal = None

from app.db.base import session_scope  # noqa: E402
from app.db.models import GenerationBlock, WeatherBlock  # noqa: E402
from app.db.seed import run_seed  # noqa: E402
from app.main import app  # noqa: E402
from app.schedule import ensure_schedule  # noqa: E402

ADMIN = {"X-API-Key": "test-admin-key"}
PAST = date.today() - timedelta(days=3)
FUTURE = date.today() + timedelta(days=2)


def wrapper_headers() -> dict:
    """Whatever wrapper key is actually configured in this process.

    Env vars are process-global and pytest imports every test module before
    running, so a sibling module (test_wrapper.py) can win the race and set a
    key. Reading the live setting keeps these tests independent of import order.
    """
    key = settings_mod.get_settings().RENEWABLE_WRAPPER_USER_API_KEY
    return {"X-API-Key": key} if key else {}


def _seed_generation(sim_date: date, mode: str = "HISTORICAL") -> None:
    """Write a physically-shaped day of generation + weather straight to the DB."""
    import math
    from datetime import datetime

    with session_scope() as db:
        for n in range(1, 97):
            hour = (n - 1) * 0.25
            solar = (160.0 * max(0.0, math.sin(math.pi * (hour - 6) / 12))
                     if 6 <= hour <= 18 else 0.0)
            wind = 40.0 + 25.0 * math.sin(2 * math.pi * hour / 24)
            start = datetime.combine(sim_date, datetime.min.time()) + timedelta(minutes=15 * (n - 1))
            db.add(GenerationBlock(
                plant_code="HYBRID01", sim_date=sim_date, block_no=n,
                block_start=start, block_end=start + timedelta(minutes=15),
                solar_mw=solar, solar_mwh=solar * 0.25,
                wind_mw=wind, wind_mwh=wind * 0.25,
                total_mw=solar + wind, total_mwh=(solar + wind) * 0.25,
                solar_cuf=0.0, wind_cuf=0.0, hybrid_cuf=0.0,
                solar_status="OK", wind_status="OK",
                data_mode=mode, data_source="test", data_label=f"{mode}_SIMULATED",
                data_quality_status="OK", simulation_version="v1.0.0",
                model_assumption_version="v1.0.0", plant_config_version=1,
                weather_source="test", is_current=True,
            ))
            db.add(WeatherBlock(
                plant_code="HYBRID01", sim_date=sim_date, block_no=n,
                block_start=start, block_end=start + timedelta(minutes=15),
                data_mode=mode, weather_source="test", interpolated=False,
                cloud_cover=35.0, wind_speed_100m=8.0,
            ))


@pytest.fixture(scope="module")
def client():
    run_seed()
    _seed_generation(PAST, "HISTORICAL")
    _seed_generation(FUTURE, "FORECAST")
    ensure_schedule("HYBRID01", PAST)
    ensure_schedule("HYBRID01", FUTURE)
    with TestClient(app) as c:
        yield c


# --- auth -------------------------------------------------------------------
def test_schedule_requires_key(client):
    assert client.get(f"/plants/HYBRID01/schedule?date={PAST}").status_code == 401


def test_existing_read_key_works(client):
    """Credentials already issued to consumers must reach the new endpoint."""
    r = client.post("/admin/api-keys", headers=ADMIN,
                    json={"team": "client", "name": "existing", "scope": "read"})
    key = r.json()["api_key"]
    assert client.get(f"/plants/HYBRID01/schedule?date={PAST}",
                      headers={"X-API-Key": key}).status_code == 200


# --- payload ----------------------------------------------------------------
def test_schedule_returns_96_blocks_solar_wind_and_total(client):
    d = client.get(f"/plants/HYBRID01/schedule?date={PAST}", headers=ADMIN).json()
    assert d["block_count"] == 96 and len(d["blocks"]) == 96
    for b in d["blocks"]:
        assert b["total_p90_mw"] == pytest.approx(
            b["solar_p90_mw"] + b["wind_p90_mw"], abs=1e-3)
    assert d["solar_p90_mwh_total"] > 0
    assert d["wind_p90_mwh_total"] > 0
    assert d["total_p90_mwh_total"] == pytest.approx(
        d["solar_p90_mwh_total"] + d["wind_p90_mwh_total"], abs=0.5)


def test_missing_schedule_is_404(client):
    far = date.today() - timedelta(days=900)
    assert client.get(f"/plants/HYBRID01/schedule?date={far}",
                      headers=ADMIN).status_code == 404


def test_accuracy_endpoint(client):
    d = client.get(f"/plants/HYBRID01/schedule/accuracy?date={PAST}", headers=ADMIN).json()
    assert d["blocks_compared"] == 96
    assert 0 <= d["nmae_pct_capacity"] < 15
    assert abs(d["day_deviation_pct"]) <= 6


def test_range_endpoint(client):
    r = client.get(f"/plants/HYBRID01/schedule/range?start={PAST}&end={PAST}", headers=ADMIN)
    assert r.status_code == 200 and len(r.json()) == 1


def test_range_rejects_oversized_window(client):
    r = client.get(
        f"/plants/HYBRID01/schedule/range?start={PAST}&end={PAST + timedelta(days=40)}",
        headers=ADMIN)
    assert r.status_code == 400


# --- freeze semantics -------------------------------------------------------
def test_schedule_is_frozen_once_issued(client):
    before = client.get(f"/plants/HYBRID01/schedule?date={PAST}", headers=ADMIN).json()
    res = ensure_schedule("HYBRID01", PAST)          # second call, no force
    assert res["issued"] is False and res["reason"] == "frozen"
    after = client.get(f"/plants/HYBRID01/schedule?date={PAST}", headers=ADMIN).json()
    assert before["blocks"] == after["blocks"]


def test_force_reissues(client):
    assert ensure_schedule("HYBRID01", PAST, force=True)["issued"] is True


# --- wrapper policy ---------------------------------------------------------
def test_wrapper_serves_past_schedule(client):
    d = client.get(f"/api/renewable/schedule?date={PAST}",
                   headers=wrapper_headers()).json()
    assert len(d["blocks"]) == 96
    assert {"solar_p90_mw", "wind_p90_mw", "total_p90_mw"} <= set(d["blocks"][0])


def test_wrapper_rejects_future_schedule(client):
    """The wrapper promises no forward-looking data; a future schedule is exactly that."""
    r = client.get(f"/api/renewable/schedule?date={FUTURE}", headers=wrapper_headers())
    assert r.status_code == 400


def test_wrapper_csv_export(client):
    r = client.get(f"/api/renewable/schedule?date={PAST}&format=csv",
                   headers=wrapper_headers())
    assert r.status_code == 200
    assert "solar_p90_mw" in r.text.splitlines()[0]


# --- backward compatibility -------------------------------------------------
def test_existing_endpoints_unchanged(client):
    """The pre-existing response shapes must not gain or lose a single field."""
    hist = client.get(f"/plants/HYBRID01/historical?date={PAST}", headers=ADMIN).json()
    assert set(hist["blocks"][0]) == {
        "block_no", "block_start", "block_end", "solar_mw", "solar_mwh",
        "wind_mw", "wind_mwh", "total_mw", "total_mwh", "solar_cuf", "wind_cuf",
        "hybrid_cuf", "solar_status", "wind_status", "data_mode", "data_label",
        "data_quality_status",
    }
    wrapped = client.get(f"/api/renewable/historical?date={PAST}",
                         headers=wrapper_headers()).json()
    assert set(wrapped) == {"plant_id", "date", "data_policy", "blocks"}
    assert set(wrapped["blocks"][0]) == {
        "sim_date", "block_no", "block_start", "block_end", "solar_mw", "wind_mw",
        "total_mw", "solar_mwh", "wind_mwh", "total_mwh", "data_label",
    }
