"""Tests for the data-quality checks."""
from __future__ import annotations

import dataclasses

from app.engines.hybrid import simulate_day
from app.quality import check_day


def test_clean_day_passes(spec, synthetic_day):
    results = simulate_day(spec, synthetic_day)
    report = check_day(spec, results)
    assert report.status in ("OK", "PARTIAL")
    assert not report.issues


def test_missing_block_fails(spec, synthetic_day):
    results = simulate_day(spec, synthetic_day)[:-1]  # drop one block
    report = check_day(spec, results)
    assert report.status == "FAILED"
    assert any("missing" in i or "96" in i for i in report.issues)


def test_negative_generation_fails(spec, synthetic_day):
    results = simulate_day(spec, synthetic_day)
    results[10] = dataclasses.replace(results[10], wind_mw=-5.0, total_mw=-5.0)
    report = check_day(spec, results)
    assert report.status == "FAILED"
    assert any("negative" in i for i in report.issues)


def test_solar_at_night_fails(spec, synthetic_day):
    results = simulate_day(spec, synthetic_day)
    # Force a NIGHT-status block to report non-zero solar.
    night_idx = next(i for i, r in enumerate(results) if r.solar_status == "NIGHT")
    results[night_idx] = dataclasses.replace(
        results[night_idx], solar_mw=10.0, total_mw=10.0
    )
    report = check_day(spec, results)
    assert report.status == "FAILED"
    assert any("night" in i for i in report.issues)


def test_cap_exceeded_fails(spec, synthetic_day):
    results = simulate_day(spec, synthetic_day)
    results[48] = dataclasses.replace(results[48], solar_mw=999.0, total_mw=999.0)
    report = check_day(spec, results)
    assert report.status == "FAILED"
