"""Day-ahead P90 schedule engine tests.

Covers the physical invariants, determinism, accuracy calibration, and the
low-output regression that an earlier additive-Gaussian formulation produced.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from app.engines.hybrid import simulate_day
from app.engines.p90 import ScheduleParams, build_schedule

SIM_DATE = date(2026, 6, 21)


@pytest.fixture
def schedule(spec, synthetic_day):
    results = simulate_day(spec, synthetic_day)
    return build_schedule(spec, results, synthetic_day, SIM_DATE), results


def test_produces_96_blocks(schedule):
    blocks, _ = schedule
    assert len(blocks) == 96
    assert [b.block_no for b in blocks] == list(range(1, 97))


def test_deterministic(spec, synthetic_day):
    results = simulate_day(spec, synthetic_day)
    a = build_schedule(spec, results, synthetic_day, SIM_DATE)
    b = build_schedule(spec, results, synthetic_day, SIM_DATE)
    assert [x.total_p90_mw for x in a] == [x.total_p90_mw for x in b]


def test_different_dates_differ(spec, synthetic_day):
    results = simulate_day(spec, synthetic_day)
    a = build_schedule(spec, results, synthetic_day, date(2026, 6, 21))
    b = build_schedule(spec, results, synthetic_day, date(2026, 6, 22))
    assert [x.total_p90_mw for x in a] != [x.total_p90_mw for x in b]


def test_solar_zero_at_night(schedule):
    blocks, results = schedule
    for b, r in zip(blocks, results, strict=False):
        if r.solar_mw <= 0.0:
            assert b.solar_p90_mw == 0.0, f"block {b.block_no} scheduled solar at night"


def test_caps_respected(spec, schedule):
    blocks, _ = schedule
    for b in blocks:
        assert 0.0 <= b.solar_p90_mw <= spec.solar_ac_mw + 1e-9
        assert 0.0 <= b.wind_p90_mw <= spec.wind_ac_mw + 1e-9


def test_total_is_sum_of_components(schedule):
    blocks, _ = schedule
    for b in blocks:
        assert b.total_p90_mw == pytest.approx(b.solar_p90_mw + b.wind_p90_mw, abs=1e-9)


def test_mwh_consistent_with_mw(spec, schedule):
    blocks, _ = schedule
    hours = spec.block_minutes / 60.0
    for b in blocks:
        assert b.total_p90_mwh == pytest.approx(b.total_p90_mw * hours, abs=1e-9)


def test_band_brackets_the_schedule(schedule):
    blocks, _ = schedule
    for b in blocks:
        assert b.total_band_low_mw <= b.total_p90_mw + 1e-6
        assert b.total_band_high_mw >= b.total_p90_mw - 1e-6
        assert b.solar_band_low_mw >= 0.0
        assert b.wind_band_low_mw >= 0.0


def test_day_energy_within_cap(schedule):
    """The schedule's day total must stay close to actual — a desk checks it."""
    blocks, results = schedule
    actual = sum(r.total_mwh for r in results)
    sched = sum(b.total_p90_mwh for b in blocks)
    assert abs(sched - actual) / actual <= 0.06


def test_error_is_not_uniform(schedule):
    """Guards the core realism property: a flat band is synthetic-looking.

    Relative error must vary materially across the day rather than sitting at a
    constant percentage.
    """
    blocks, results = schedule
    rel = [
        abs(b.total_p90_mw - r.total_mw) / r.total_mw
        for b, r in zip(blocks, results, strict=False)
        if r.total_mw > 5.0
    ]
    assert np.std(rel) > 0.02, "error looks uniform across the day"


def test_no_sustained_flatline_at_zero(spec, synthetic_day):
    """Regression: additive-Gaussian + clip(0) pinned the schedule at exactly 0
    for hours while the plant was genuinely generating. Sweep many dates so a
    tail excursion in the error process is actually exercised."""
    results = simulate_day(spec, synthetic_day)
    worst = 0
    for day in range(1, 61):
        blocks = build_schedule(spec, results, synthetic_day, date(2026, 4, 1)
                                + __import__("datetime").timedelta(days=day))
        run = 0
        for b, r in zip(blocks, results, strict=False):
            if r.total_mw > 10.0 and b.total_p90_mw < 0.5 * r.total_mw:
                run += 1
                worst = max(worst, run)
            else:
                run = 0
    assert worst <= 4, f"schedule collapsed below half of actual for {worst} blocks"


def test_accuracy_in_target_range(spec, synthetic_day):
    """Calibration: ~90% accurate overall, averaged over many days."""
    results = simulate_day(spec, synthetic_day)
    cap = spec.solar_ac_mw + spec.wind_ac_mw
    nmaes = []
    for day in range(40):
        blocks = build_schedule(
            spec, results, synthetic_day,
            date(2026, 3, 1) + __import__("datetime").timedelta(days=day))
        errs = [abs(b.total_p90_mw - r.total_mw) for b, r in zip(blocks, results, strict=False)]
        nmaes.append(sum(errs) / len(errs) / cap * 100)
    mean_nmae = float(np.mean(nmaes))
    assert 0.5 < mean_nmae < 8.0, f"nMAE {mean_nmae:.2f}% of capacity is out of range"


def test_sigma_scale_widens_error(spec, synthetic_day):
    """The master knob must actually control accuracy."""
    results = simulate_day(spec, synthetic_day)

    def nmae(scale):
        blocks = build_schedule(spec, results, synthetic_day, SIM_DATE,
                                ScheduleParams(sigma_scale=scale))
        return sum(abs(b.total_p90_mw - r.total_mw)
                   for b, r in zip(blocks, results, strict=False)) / len(blocks)

    assert nmae(2.5) > nmae(0.5)
