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
    assert "Renewable Generation Dashboard" in r.text
