"""API auth / authorization / data-label tests (no network required)."""
from __future__ import annotations

import os
import tempfile

# Configure an isolated test DB + disable scheduler BEFORE importing app modules.
_TMPDIR = tempfile.mkdtemp(prefix="rensim_test_")
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

from app.db.seed import run_seed  # noqa: E402
from app.main import app  # noqa: E402

ADMIN = {"X-API-Key": "test-admin-key"}


@pytest.fixture(scope="module")
def client():
    run_seed()
    with TestClient(app) as c:
        yield c


def test_health_open(client):
    assert client.get("/health").status_code == 200


def test_config_requires_key(client):
    assert client.get("/plants/HYBRID01/config").status_code == 401


def test_config_with_valid_key(client):
    r = client.get("/plants/HYBRID01/config", headers=ADMIN)
    assert r.status_code == 200
    body = r.json()
    assert body["solar_ac_mw"] == 160.0
    assert body["wind_ac_mw"] == 135.0
    assert body["dc_ac_ratio"] == 1.5
    # No internal IDs leaked.
    assert "id" not in body and "plant_id" not in body


def test_invalid_key_rejected(client):
    r = client.get("/plants/HYBRID01/config", headers={"X-API-Key": "nope"})
    assert r.status_code == 401


def test_read_key_cannot_access_admin(client):
    # Mint a read-only key via admin, then attempt an admin call with it.
    r = client.post(
        "/admin/api-keys",
        headers=ADMIN,
        json={"team": "ext", "name": "reader", "scope": "read"},
    )
    assert r.status_code == 200
    read_key = r.json()["api_key"]
    # Read endpoint OK.
    assert client.get("/plants/HYBRID01/config", headers={"X-API-Key": read_key}).status_code == 200
    # Admin endpoint forbidden.
    r2 = client.post(
        "/admin/reprocess",
        headers={"X-API-Key": read_key},
        json={"plant_code": "HYBRID01", "dates": ["2024-01-01"]},
    )
    assert r2.status_code == 403


def test_admin_endpoint_needs_key(client):
    r = client.post("/admin/reprocess", json={"plant_code": "HYBRID01", "dates": ["2024-01-01"]})
    assert r.status_code == 401


def test_missing_data_returns_404_after_auth(client):
    # Auth passes (valid key) but no simulation exists yet -> 404, not 401.
    r = client.get("/plants/HYBRID01/historical?date=2020-01-01", headers=ADMIN)
    assert r.status_code == 404


def test_key_listing_and_revocation(client):
    r = client.post(
        "/admin/api-keys", headers=ADMIN,
        json={"team": "t", "name": "temp", "scope": "read", "rate_limit_per_min": 50},
    )
    prefix = r.json()["key_prefix"]
    lst = client.get("/admin/api-keys", headers=ADMIN).json()
    assert any(k["key_prefix"] == prefix for k in lst["keys"])
    rev = client.delete(f"/admin/api-keys/{prefix}", headers=ADMIN)
    assert rev.status_code == 200


def test_dashboard_loads(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "Renewable Generation Platform" in r.text


def test_config_update_requires_admin(client):
    # No key -> 401.
    assert client.put("/plants/HYBRID01/config", json={"panel_tilt": 30}).status_code == 401
    # Read key -> 403.
    r = client.post(
        "/admin/api-keys", headers=ADMIN,
        json={"team": "x", "name": "r2", "scope": "read"},
    )
    rk = r.json()["api_key"]
    assert client.put(
        "/plants/HYBRID01/config", headers={"X-API-Key": rk}, json={"panel_tilt": 30}
    ).status_code == 403


def test_config_update_creates_new_version(client):
    before = client.get("/plants/HYBRID01/config", headers=ADMIN).json()["config_version"]
    r = client.put(
        "/plants/HYBRID01/config", headers=ADMIN,
        json={"solar_ac_mw": 170, "solar_dc_mw": 255, "latitude": 27.5},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["config_version"] == before + 1
    assert body["solar_ac_mw"] == 170
    assert body["latitude"] == 27.5
    assert abs(body["dc_ac_ratio"] - 1.5) < 1e-6  # recomputed 255/170


def test_weather_endpoint_auth_and_404(client):
    # Auth required.
    assert client.get("/plants/HYBRID01/weather?date=2020-01-01").status_code == 401
    # Valid key, no data -> 404 (not 401).
    assert client.get(
        "/plants/HYBRID01/weather?date=2020-01-01", headers=ADMIN
    ).status_code == 404


def test_dashboard_open_feeds(client):
    assert client.get("/dashboard/api/config/HYBRID01").status_code == 200
    assert client.get(
        "/dashboard/api/day/HYBRID01?date=2020-01-01&mode=HISTORICAL"
    ).status_code == 200  # open feed returns empty blocks, not 404
    assert client.get(
        "/dashboard/api/weather/HYBRID01?date=2020-01-01&mode=LIVE"
    ).status_code == 200


def test_ensure_fresh_live_skips_when_recent(client):
    """If today's LIVE data is fresh, ensure_fresh_live must NOT refetch (no network)."""
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    from app.db.base import session_scope
    from app.db.models import GenerationBlock
    from app.simulate import ensure_fresh_live

    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    midnight = datetime(today.year, today.month, today.day)
    with session_scope() as db:
        db.add(
            GenerationBlock(
                plant_code="HYBRID01", sim_date=today, block_no=1,
                block_start=midnight, block_end=midnight,
                data_mode="LIVE", data_source="test", data_label="LIVE_ESTIMATED",
                data_quality_status="OK", simulation_version="v1.0.0",
                model_assumption_version="v1.0.0", plant_config_version=1,
                weather_source="test", weather_fetch_time=datetime.now(UTC),
                is_current=True,
            )
        )
    res = ensure_fresh_live("HYBRID01")
    assert res["refreshed"] is False
    assert res.get("age_seconds") is not None and res["age_seconds"] < 60


def test_dashboard_config_update_no_key(client):
    """The dashboard console edits config WITHOUT an API key (trusted same-origin)."""
    before = client.get("/dashboard/api/config/HYBRID01").json()["config_version"]
    r = client.put(
        "/dashboard/api/config/HYBRID01",
        json={"solar_ac_mw": 165, "solar_dc_mw": 247.5, "latitude": 26.8},
    )
    assert r.status_code == 200
    assert r.json()["config_version"] == before + 1
    after = client.get("/dashboard/api/config/HYBRID01").json()
    assert after["solar_ac_mw"] == 165 and after["latitude"] == 26.8


def test_dashboard_key_lifecycle_no_admin_key(client):
    """Generate / list / revoke API keys from the console without entering a key."""
    r = client.post(
        "/dashboard/api/keys",
        json={"team": "ext", "name": "consumer", "scope": "read", "rate_limit_per_min": 60},
    )
    assert r.status_code == 200
    raw = r.json()["api_key"]
    prefix = r.json()["key_prefix"]
    # The minted key actually works against the protected API.
    assert client.get(
        "/plants/HYBRID01/config", headers={"X-API-Key": raw}
    ).status_code == 200
    # It shows up in the console listing and can be revoked.
    assert any(k["key_prefix"] == prefix for k in client.get("/dashboard/api/keys").json()["keys"])
    assert client.delete(f"/dashboard/api/keys/{prefix}").status_code == 200
    # Revoked key no longer authorizes.
    assert client.get(
        "/plants/HYBRID01/config", headers={"X-API-Key": raw}
    ).status_code == 401
