"""Tests for the `backfill` CLI command (DB + weather mocked, no network).

Verifies date enumeration, skipping dates already present, --force, --dry-run,
per-date failure resilience, and that backfilled days are written with
triggered_by="backfill" (so they get the normal HISTORICAL_SIMULATED label,
NOT the REPROCESSED label that /admin/reprocess produces).
"""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

# Isolated SQLite DB so settings never point tests at a real/prod database.
_TMPDIR = tempfile.mkdtemp(prefix="rensim_backfill_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDIR}/test.db".replace("\\", "/")
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["PLANT_CODE"] = "HYBRID01"

import pytest  # noqa: E402

import app.api.repository as repo_mod  # noqa: E402
import app.config.settings as settings_mod  # noqa: E402
import app.db.base as db_base  # noqa: E402
import app.simulate as simulate_mod  # noqa: E402
from app.cli import main  # noqa: E402
from app.weather.client import DataMode  # noqa: E402

settings_mod.get_settings.cache_clear()


@pytest.fixture
def fake_runs(monkeypatch):
    """Record run_simulation_sync calls instead of hitting weather/DB; stub the DB scope."""
    calls: list[dict] = []

    def fake_run(plant, sim_date, mode, triggered_by="manual", force_refetch=False, **kw):
        calls.append(
            {
                "plant": plant, "date": sim_date, "mode": mode,
                "triggered_by": triggered_by, "force_refetch": force_refetch,
            }
        )
        return SimpleNamespace(
            sim_date=sim_date, mode=mode.value, data_label="HISTORICAL_SIMULATED",
            quality_status="OK", total_mwh=123.4, blocks_written=96,
        )

    monkeypatch.setattr(simulate_mod, "run_simulation_sync", fake_run)

    @contextmanager
    def fake_scope():
        yield None

    monkeypatch.setattr(db_base, "session_scope", fake_scope)
    return calls


def _present(monkeypatch, dates: set[date]):
    monkeypatch.setattr(repo_mod, "get_present_dates", lambda *a, **k: set(dates))


def test_backfill_dry_run_makes_no_runs(fake_runs, monkeypatch, capsys):
    _present(monkeypatch, set())
    rc = main(["backfill", "--start", "2026-04-26", "--end", "2026-04-28", "--dry-run"])
    assert rc == 0
    assert fake_runs == []  # nothing executed
    out = capsys.readouterr().out
    assert "3 to process" in out
    assert "2026-04-26" in out and "2026-04-28" in out


def test_backfill_skips_present_dates(fake_runs, monkeypatch):
    _present(monkeypatch, {date(2026, 4, 27)})
    rc = main(["backfill", "--start", "2026-04-26", "--end", "2026-04-28"])
    assert rc == 0
    done = [c["date"] for c in fake_runs]
    assert done == [date(2026, 4, 26), date(2026, 4, 28)]  # 04-27 skipped
    # Every run is HISTORICAL, force-refetched, and labelled as a backfill (not reprocess).
    assert all(c["mode"] is DataMode.HISTORICAL for c in fake_runs)
    assert all(c["force_refetch"] is True for c in fake_runs)
    assert all(c["triggered_by"] == "backfill" for c in fake_runs)


def test_backfill_force_reruns_present_dates(fake_runs, monkeypatch):
    # get_present_dates must NOT even be consulted with --force.
    def boom(*a, **k):  # pragma: no cover - should never run
        raise AssertionError("get_present_dates called despite --force")

    monkeypatch.setattr(repo_mod, "get_present_dates", boom)
    rc = main(["backfill", "--start", "2026-04-26", "--end", "2026-04-28", "--force"])
    assert rc == 0
    assert [c["date"] for c in fake_runs] == [
        date(2026, 4, 26), date(2026, 4, 27), date(2026, 4, 28),
    ]


def test_backfill_continues_on_failure_and_reports(monkeypatch, capsys):
    _present(monkeypatch, set())

    @contextmanager
    def fake_scope():
        yield None

    monkeypatch.setattr(db_base, "session_scope", fake_scope)

    def flaky(plant, sim_date, mode, triggered_by="manual", force_refetch=False, **kw):
        if sim_date == date(2026, 4, 27):
            raise RuntimeError("open-meteo boom")
        return SimpleNamespace(
            sim_date=sim_date, mode=mode.value, data_label="HISTORICAL_SIMULATED",
            quality_status="OK", total_mwh=1.0, blocks_written=96,
        )

    monkeypatch.setattr(simulate_mod, "run_simulation_sync", flaky)
    rc = main(["backfill", "--start", "2026-04-26", "--end", "2026-04-28"])
    assert rc == 1  # a failure => non-zero exit
    out = capsys.readouterr().out
    assert "1 failed" in out
    assert "2026-04-27" in out and "open-meteo boom" in out


def test_backfill_rejects_inverted_range(fake_runs, monkeypatch):
    _present(monkeypatch, set())
    rc = main(["backfill", "--start", "2026-04-28", "--end", "2026-04-26"])
    assert rc == 2
    assert fake_runs == []


def test_backfill_rejects_future_historical_dates(fake_runs, monkeypatch, capsys):
    _present(monkeypatch, set())
    # Far-future end date => HISTORICAL backfill must refuse (no fabricated history).
    rc = main(["backfill", "--start", "2099-01-01", "--end", "2099-01-03"])
    assert rc == 2
    assert fake_runs == []
    assert "future" in capsys.readouterr().out.lower()
