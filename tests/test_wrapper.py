"""Tests for the restricted user-facing wrapper API (provider mocked, no network).

Covers the testing checklist: live data, forecast blocking, future-date rejection,
range limits, CSV output, wrapper-key auth, provider-config + provider-failure
errors, and the no-key-leak guarantee.
"""
from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta

# Configure env BEFORE importing app modules.
_TMPDIR = tempfile.mkdtemp(prefix="rensim_wrap_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDIR}/test.db".replace("\\", "/")
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["ADMIN_BOOTSTRAP_KEY"] = "test-admin-key"
os.environ["PLANT_CODE"] = "HYBRID01"
os.environ["RENEWABLE_API_BASE_URL"] = "https://provider.example"
os.environ["RENEWABLE_API_KEY"] = "SECRET-EXTERNAL-KEY"   # must never leak to users
os.environ["RENEWABLE_PLANT_ID"] = "HYBRID01"
os.environ["RENEWABLE_PLANT_TZ"] = "Asia/Kolkata"
os.environ["RENEWABLE_WRAPPER_USER_API_KEY"] = "wrap-user-key"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.config.settings as settings_mod  # noqa: E402
import app.db.base as db_base  # noqa: E402

settings_mod.get_settings.cache_clear()
db_base._engine = None
db_base._SessionLocal = None

from app.db.seed import run_seed  # noqa: E402
from app.main import app  # noqa: E402
from app.wrapper import client as provider  # noqa: E402

USER = {"X-API-Key": "wrap-user-key"}
PAST = (date.today() - timedelta(days=5)).isoformat()
FUTURE = (date.today() + timedelta(days=5)).isoformat()


@pytest.fixture(scope="module")
def client():
    run_seed()
    with TestClient(app) as c:
        yield c


def _block(no, label, **kw):
    b = {
        "block_no": no, "block_start": f"2026-06-20T{(no-1)//4:02d}:00:00",
        "block_end": f"2026-06-20T{(no-1)//4:02d}:15:00",
        "solar_mw": 10.0, "wind_mw": 5.0, "total_mw": 15.0,
        "solar_mwh": 2.5, "wind_mwh": 1.25, "total_mwh": 3.75, "data_label": label,
    }
    b.update(kw)
    return b


# 1) /current returns live estimated data ------------------------------------
def test_current_returns_live(client, monkeypatch):
    monkeypatch.setattr(provider, "fetch_current", lambda p: {
        "plant_code": p, "plant_name": "Secret Internal Name", "timezone": "Asia/Kolkata",
        "block_no": 56, "block_start": "2026-06-26T14:00:00", "block_end": "2026-06-26T14:15:00",
        "solar_mw": 96.88, "wind_mw": 8.12, "total_mw": 105.0, "energy_today_mwh": 653.5,
        "hybrid_cuf": 0.42, "data_label": "LIVE_ESTIMATED", "as_of": "2026-06-26T14:01:00Z",
        "refresh_interval_minutes": 15, "note": "internal note",
    })
    r = client.get("/api/renewable/current", headers=USER)
    assert r.status_code == 200
    body = r.json()
    assert body["plant_id"] == "HYBRID01"
    assert body["data_label"] == "LIVE_ESTIMATED"
    assert body["total_mw"] == 105.0
    # No provider internals leaked.
    assert "plant_name" not in body and "note" not in body and "data_quality_status" not in body


def test_current_rejects_forecast(client, monkeypatch):
    monkeypatch.setattr(provider, "fetch_current", lambda p: {
        "block_no": 80, "data_label": "FORECAST_SIMULATED", "total_mw": 50.0,
    })
    r = client.get("/api/renewable/current", headers=USER)
    assert r.status_code == 403


# 3) today-completed-blocks strips forecast + future blocks ------------------
def test_today_blocks_no_forecast(client, monkeypatch):
    monkeypatch.setattr(provider, "fetch_live", lambda p: {
        "sim_date": "2026-06-26", "current_block_no": 56,
        "blocks": [
            _block(55, "LIVE_ESTIMATED"),
            _block(56, "LIVE_ESTIMATED"),
            _block(57, "FORECAST_SIMULATED"),   # future label -> dropped
            _block(80, "FORECAST_SIMULATED"),   # future label -> dropped
        ],
    })
    r = client.get("/api/renewable/today-completed-blocks", headers=USER)
    assert r.status_code == 200
    body = r.json()
    assert body["data_policy"] == "LIVE_AND_HISTORICAL_ONLY_NO_FORECAST"
    labels = {b["data_label"] for b in body["blocks"]}
    assert labels == {"LIVE_ESTIMATED"}
    assert all(b["block_no"] <= 56 for b in body["blocks"])
    assert len(body["blocks"]) == 2


# 4) historical rejects future date ------------------------------------------
def test_historical_rejects_future(client):
    r = client.get(f"/api/renewable/historical?date={FUTURE}", headers=USER)
    assert r.status_code == 400


def test_historical_strips_forecast(client, monkeypatch):
    monkeypatch.setattr(provider, "fetch_historical", lambda p, d: {
        "sim_date": d, "blocks": [_block(1, "HISTORICAL_SIMULATED"), _block(2, "FORECAST_SIMULATED")],
    })
    r = client.get(f"/api/renewable/historical?date={PAST}", headers=USER)
    assert r.status_code == 200
    labels = {b["data_label"] for b in r.json()["blocks"]}
    assert labels == {"HISTORICAL_SIMULATED"}


# 5) range rejects future + oversize -----------------------------------------
def test_range_rejects_future_and_oversize(client):
    assert client.get(f"/api/renewable/range?start={PAST}&end={FUTURE}", headers=USER).status_code == 400
    big_start = (date.today() - timedelta(days=60)).isoformat()
    big_end = (date.today() - timedelta(days=1)).isoformat()
    assert client.get(f"/api/renewable/range?start={big_start}&end={big_end}", headers=USER).status_code == 400
    # start > end
    assert client.get(f"/api/renewable/range?start={PAST}&end={big_start}", headers=USER).status_code == 400


# 6) CSV output is flat + Excel-friendly -------------------------------------
def test_csv_output(client, monkeypatch):
    monkeypatch.setattr(provider, "fetch_live", lambda p: {
        "sim_date": "2026-06-26", "current_block_no": 2,
        "blocks": [_block(1, "LIVE_ESTIMATED"), _block(2, "LIVE_ESTIMATED"), _block(3, "FORECAST_SIMULATED")],
    })
    r = client.get("/api/renewable/today-completed-blocks?format=csv", headers=USER)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("sim_date,block_no,block_start")
    assert len(lines) == 3  # header + 2 live blocks
    assert "FORECAST_SIMULATED" not in r.text
    assert "{" not in r.text  # no nested objects


# 7) wrong / missing wrapper key ---------------------------------------------
def test_wrong_wrapper_key(client):
    assert client.get("/api/renewable/current", headers={"X-API-Key": "nope"}).status_code == 401
    assert client.get("/api/renewable/current").status_code == 401  # missing


# 8) missing external provider key -> clear config error ---------------------
def test_provider_not_configured(client, monkeypatch):
    def _raise(*a, **k):
        raise provider.ProviderNotConfigured("RENEWABLE_API_KEY is not set")
    monkeypatch.setattr(provider, "fetch_current", _raise)
    r = client.get("/api/renewable/current", headers=USER)
    assert r.status_code == 500
    assert "not configured" in r.json()["detail"].lower()


# 9) provider failure -> clean 502 -------------------------------------------
def test_provider_failure_502(client, monkeypatch):
    def _raise(*a, **k):
        raise provider.ProviderError(502, "Renewable data provider unavailable")
    monkeypatch.setattr(provider, "fetch_current", _raise)
    r = client.get("/api/renewable/current", headers=USER)
    assert r.status_code == 502
    assert r.json()["detail"] == "Renewable data provider unavailable"


# 10) the external key never leaks -------------------------------------------
def test_no_key_leak(client, monkeypatch):
    monkeypatch.setattr(provider, "fetch_current", lambda p: {
        "block_no": 1, "data_label": "LIVE_ESTIMATED", "total_mw": 1.0,
    })
    r = client.get("/api/renewable/current", headers=USER)
    assert "SECRET-EXTERNAL-KEY" not in r.text
