"""Free-tier resilience: what happens when Open-Meteo rate-limits us (HTTP 429).

Reproduces the production incident these behaviours exist to prevent: the provider
starts returning 429, today's LIVE simulation fails, no blocks are written, and every
consumer read 404s while simultaneously launching another 4-call fetch — which keeps the
quota exhausted. No network is used anywhere in this module.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

# Configure env BEFORE importing app modules.
_TMPDIR = tempfile.mkdtemp(prefix="rensim_ratelimit_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDIR}/test.db".replace("\\", "/")
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["ADMIN_BOOTSTRAP_KEY"] = "test-admin-key"
os.environ["PLANT_CODE"] = "HYBRID01"
os.environ["RENEWABLE_PLANT_ID"] = "HYBRID01"
os.environ["RENEWABLE_PLANT_TZ"] = "Asia/Kolkata"
# Left unset deliberately: mirrors production, where the wrapper key is not configured.
os.environ.pop("RENEWABLE_WRAPPER_USER_API_KEY", None)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.config.settings as settings_mod  # noqa: E402
import app.db.base as db_base  # noqa: E402

settings_mod.get_settings.cache_clear()
db_base._engine = None
db_base._SessionLocal = None

import app.simulate as simulate_mod  # noqa: E402
from app.db.base import session_scope  # noqa: E402
from app.db.models import RawWeatherResponse  # noqa: E402
from app.db.seed import run_seed  # noqa: E402
from app.main import app  # noqa: E402
from app.weather.client import WeatherFetchError, _get_with_retry  # noqa: E402
from app.weather.store import covers_date, find_raw_covering  # noqa: E402

PLANT = "HYBRID01"
TZ = ZoneInfo("Asia/Kolkata")


# --- fakes -------------------------------------------------------------------
class _Resp:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.request = None

    def raise_for_status(self) -> None:  # pragma: no cover - only 2xx path
        pass


class _CountingClient:
    """Minimal stand-in for httpx.AsyncClient that always returns `status`."""

    def __init__(self, status: int):
        self.status = status
        self.calls = 0

    async def get(self, url, params=None):
        self.calls += 1
        return _Resp(self.status)


def _hourly_raw(start: date, end: date) -> dict:
    """A well-formed Open-Meteo hourly response spanning [start, end] inclusive."""
    times: list[str] = []
    t = datetime.combine(start, time.min)
    last = datetime.combine(end, time(23, 0))
    while t <= last:
        times.append(t.isoformat(timespec="minutes"))
        t += timedelta(hours=1)
    n = len(times)

    def solar(i: int) -> float:
        hour = i % 24
        return 800.0 if 7 <= hour <= 17 else 0.0

    return {
        "hourly": {
            "time": times,
            "shortwave_radiation": [solar(i) for i in range(n)],
            "direct_radiation": [solar(i) * 0.8 for i in range(n)],
            "diffuse_radiation": [solar(i) * 0.2 for i in range(n)],
            "direct_normal_irradiance": [solar(i) * 0.9 for i in range(n)],
            "temperature_2m": [28.0] * n,
            "cloud_cover": [20.0] * n,
            "is_day": [1 if solar(i) > 0 else 0 for i in range(n)],
            "wind_speed_10m": [5.0] * n,
            "wind_speed_100m": [8.0] * n,
            "wind_speed_120m": [8.2] * n,
            "wind_speed_180m": [8.5] * n,
            "wind_direction_100m": [180.0] * n,
            "wind_gusts_10m": [11.0] * n,
            "surface_pressure": [950.0] * n,
        }
    }


@pytest.fixture(scope="module")
def client():
    run_seed()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_backoff():
    """Provider-failure memory is module-level state; keep tests independent."""
    simulate_mod._live_failures.clear()
    yield
    simulate_mod._live_failures.clear()


@pytest.fixture
def stored_yesterday_response():
    """Persist a LIVE response fetched 'last night' that spans yesterday..tomorrow.

    This is what a real LIVE fetch stores (past_days=1 & forecast_days=2), and it is why
    today can be simulated with zero provider calls.
    """
    today = datetime.now(TZ).date()
    yesterday = today - timedelta(days=1)
    fetched_at = datetime.now(ZoneInfo("UTC")) - timedelta(hours=6)
    with session_scope() as db:
        db.add(
            RawWeatherResponse(
                plant_code=PLANT,
                sim_date=yesterday,
                data_mode="LIVE",
                provider="open-meteo",
                request_url="https://api.open-meteo.com/v1/forecast?stored=1",
                fetched_at=fetched_at,
                raw_json=_hourly_raw(yesterday, today + timedelta(days=1)),
            )
        )
    return today


# --- 1) the quota multiplier: 429 must not be retried ------------------------
def test_429_is_not_retried():
    """A 429 used to cost 4 provider calls; the limit that caused it can't clear in 7s."""
    fake = _CountingClient(429)
    with pytest.raises(WeatherFetchError) as excinfo:
        asyncio.run(_get_with_retry(fake, "https://x", {}, 4))
    assert fake.calls == 1, "429 must fail fast, not burn the retry budget"
    assert excinfo.value.status_code == 429
    assert excinfo.value.is_rate_limited is True


def test_5xx_still_retries(monkeypatch):
    """Genuine transient failures must still be retried — only 429 short-circuits."""
    real_sleep = asyncio.sleep  # capture before patching, or the stub recurses
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: real_sleep(0))
    fake = _CountingClient(503)
    with pytest.raises(WeatherFetchError) as excinfo:
        asyncio.run(_get_with_retry(fake, "https://x", {}, 4))
    assert fake.calls == 4
    assert excinfo.value.is_rate_limited is False


# --- 2) coverage guard on reusing a stored response --------------------------
def test_covers_date_accepts_hourly_span_and_rejects_partial():
    d = date(2026, 7, 29)
    assert covers_date(_hourly_raw(d, d), d) is True
    assert covers_date(_hourly_raw(d - timedelta(days=1), d + timedelta(days=1)), d) is True
    # A response that stops before the end of the day must be refused, otherwise
    # normalize() would flat-fill the tail from the nearest edge sample.
    partial = _hourly_raw(d, d)
    partial["hourly"]["time"] = partial["hourly"]["time"][:12]
    assert covers_date(partial, d) is False
    # A neighbouring day's response does not cover this date.
    assert covers_date(_hourly_raw(d - timedelta(days=3), d - timedelta(days=2)), d) is False
    assert covers_date({}, d) is False


def test_find_raw_covering_picks_the_stored_response(client, stored_yesterday_response):
    today = stored_yesterday_response
    with session_scope() as db:
        row = find_raw_covering(db, PLANT, today)
        assert row is not None
        assert row.provider == "open-meteo"
    with session_scope() as db:
        # Nothing stored covers a date far outside the response window.
        assert find_raw_covering(db, PLANT, today + timedelta(days=30)) is None


# --- 3) a rate-limited day still gets data ----------------------------------
def test_rate_limited_live_run_falls_back_to_stored_weather(
    client, stored_yesterday_response, monkeypatch
):
    """The incident case: provider 429s, and today must still end up with 96 blocks."""
    today = stored_yesterday_response
    calls = []

    async def always_rate_limited(*_a, **_k):
        calls.append(1)
        raise WeatherFetchError("Weather provider rate limit reached (HTTP 429)", 429)

    monkeypatch.setattr(simulate_mod, "fetch_weather", always_rate_limited)

    summary = simulate_mod.run_simulation_sync(
        PLANT, today, simulate_mod.DataMode.LIVE, triggered_by="test", force_refetch=True
    )
    assert len(calls) == 1
    assert summary.weather_from_cache is True
    assert summary.blocks_written == 96
    assert summary.fetched_fresh is False
    assert summary.total_mwh > 0
    assert "stored" in summary.weather_source

    # And the customer-facing endpoint now serves data instead of 404. Read the wrapper
    # key from live settings rather than this module's env: env is process-wide and other
    # test modules also set it, so the effective value depends on import order.
    configured = settings_mod.get_settings().RENEWABLE_WRAPPER_USER_API_KEY
    headers = {"X-API-Key": configured} if configured else {}
    r = client.get("/api/renewable/current", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["plant_id"] == PLANT
    assert body["data_label"] == "LIVE_ESTIMATED"
    assert body["block_no"] >= 1


def test_run_fails_when_nothing_covers_the_date(client, monkeypatch):
    """With no usable stored weather there is nothing to serve — the run must still fail."""

    async def always_rate_limited(*_a, **_k):
        raise WeatherFetchError("Weather provider rate limit reached (HTTP 429)", 429)

    monkeypatch.setattr(simulate_mod, "fetch_weather", always_rate_limited)
    far_future = datetime.now(TZ).date() + timedelta(days=400)
    with pytest.raises(WeatherFetchError):
        simulate_mod.run_simulation_sync(
            PLANT, far_future, simulate_mod.DataMode.FORECAST,
            triggered_by="test", force_refetch=True,
        )


# --- 4) the amplifier: a failed refresh must not re-hammer the provider ------
def test_ensure_fresh_live_backs_off_after_provider_failure(
    client, stored_yesterday_response, monkeypatch
):
    """Second and third reads during an outage must make ZERO extra provider calls.

    This is the regression that turned one 429 into a self-sustaining outage: the failed
    refresh left weather_fetch_time untouched, so the freshness gate never engaged.
    """
    calls = []

    async def always_rate_limited(*_a, **_k):
        calls.append(1)
        raise WeatherFetchError("Weather provider rate limit reached (HTTP 429)", 429)

    monkeypatch.setattr(simulate_mod, "fetch_weather", always_rate_limited)

    first = simulate_mod.ensure_fresh_live(PLANT)
    assert first["refreshed"] is True
    assert first["stale_weather"] is True
    assert len(calls) == 1

    for _ in range(5):
        again = simulate_mod.ensure_fresh_live(PLANT)
        assert again["refreshed"] is False
        assert again["provider_backoff"] is True
        assert again["retry_in_seconds"] > 0
    assert len(calls) == 1, "reads inside the backoff window must not call the provider"


def test_backoff_state_is_not_slid_by_stored_weather_runs(client, stored_yesterday_response):
    """A stored-weather-only run must not push the retry deadline out indefinitely."""
    simulate_mod._record_live_failure(PLANT)
    before = simulate_mod._live_failures[PLANT]
    remaining, _backoff = simulate_mod._failure_backoff_seconds(PLANT)
    assert remaining > 0
    # Re-running from stored weather (no network) leaves the failure record alone.
    simulate_mod.run_simulation_sync(
        PLANT, stored_yesterday_response, simulate_mod.DataMode.LIVE,
        triggered_by="test", force_refetch=True, allow_network=False,
    )
    assert simulate_mod._live_failures[PLANT] == before


def test_successful_refresh_clears_backoff(client, stored_yesterday_response, monkeypatch):
    today = stored_yesterday_response
    simulate_mod._record_live_failure(PLANT)

    class _Fetch:
        json = _hourly_raw(today - timedelta(days=1), today + timedelta(days=1))
        weather_source = "open-meteo:forecast(live)"
        fetched_at = datetime.now(ZoneInfo("UTC"))
        plant_code = PLANT
        sim_date = today
        mode = simulate_mod.DataMode.LIVE
        provider = "open-meteo"
        request_url = "https://api.open-meteo.com/v1/forecast?ok=1"
        params: dict = {}

    async def ok(*_a, **_k):
        return _Fetch()

    monkeypatch.setattr(simulate_mod, "fetch_weather", ok)
    # Force the refresh path by clearing the backoff window artificially.
    simulate_mod._live_failures[PLANT] = (
        datetime.now(ZoneInfo("UTC")) - timedelta(hours=2), 1,
    )
    res = simulate_mod.ensure_fresh_live(PLANT)
    assert res["refreshed"] is True
    assert res["stale_weather"] is False
    assert PLANT not in simulate_mod._live_failures


# --- 5) one failing step must not cancel the rest of the daily job -----------
def test_daily_job_continues_after_a_failing_step(monkeypatch):
    """A rate-limited LIVE step used to silently cancel the whole forecast horizon."""
    from app.scheduler import service as sched

    ran: list[tuple[date, str]] = []

    def fake_run(plant, sim_date, mode, triggered_by="manual", force_refetch=False, **kw):
        if mode is sched.DataMode.LIVE:
            raise WeatherFetchError("Weather provider rate limit reached (HTTP 429)", 429)
        ran.append((sim_date, mode.value))
        return None

    monkeypatch.setattr(sched, "_active_plants", lambda: [(PLANT, "Asia/Kolkata")])
    monkeypatch.setattr(sched, "run_simulation_sync", fake_run)
    sched.run_daily_job()

    modes = [m for _d, m in ran]
    assert modes.count("HISTORICAL") == 1
    assert modes.count("FORECAST") == sched.FORECAST_HORIZON_DAYS, (
        "the forecast horizon must still be built when the LIVE step fails"
    )
