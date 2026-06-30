"""Unit tests for the solar, wind and hybrid engines."""
from __future__ import annotations

from app.engines.hybrid import simulate_day
from app.engines.solar import simulate_solar_block
from app.engines.wind import simulate_wind_block
from tests.conftest import make_block


# ----------------------------- Solar --------------------------------------- #
def test_solar_zero_at_night(spec):
    r = simulate_solar_block(spec, make_block(1, poa=0.0, is_day=0))
    assert r.ac_mw == 0.0
    assert r.status == "NIGHT"


def test_solar_clips_at_ac_capacity(spec):
    # High-irradiance clear day -> DC array (240 MW) exceeds the 160 MW inverter and
    # clips to solar_ac_mw. Correct behaviour for a 1.5 DC/AC plant.
    r = simulate_solar_block(spec, make_block(48, poa=1050.0, is_day=1, temp=5.0))
    assert r.ac_mw <= spec.solar_ac_mw + 1e-9
    assert abs(r.ac_mw - spec.solar_ac_mw) < 1e-6
    assert r.status == "CLIPPED"
    assert 0.0 < r.cuf <= 1.0 + 1e-9


def test_solar_clips_at_warm_stc(spec):
    # Corrected loss-chain model (PR no longer double-counted with temp+loss): even at
    # a warm 1000 W/m^2 / 25 C noon, the 240 MW DC array exceeds the 160 MW inverter and
    # clips. Under the old triple-derate it peaked ~151 MW and never clipped.
    r = simulate_solar_block(spec, make_block(48, poa=1000.0, is_day=1, temp=25.0))
    assert r.status == "CLIPPED"
    assert abs(r.ac_mw - spec.solar_ac_mw) < 1e-6


def test_solar_partial_irradiance_below_cap(spec):
    r = simulate_solar_block(spec, make_block(40, poa=400.0, is_day=1, temp=25.0))
    assert 0.0 < r.ac_mw < spec.solar_ac_mw
    assert r.status in ("OK", "INTERPOLATED")


def test_solar_temperature_derate(spec):
    cool = simulate_solar_block(spec, make_block(40, poa=600.0, is_day=1, temp=15.0))
    hot = simulate_solar_block(spec, make_block(40, poa=600.0, is_day=1, temp=45.0))
    # Higher cell temperature reduces output (negative temp coefficient).
    assert hot.ac_mw < cool.ac_mw


# ------------------------------ Wind --------------------------------------- #
def test_wind_below_cutin_is_zero(spec):
    r = simulate_wind_block(spec, make_block(10, ws100=2.0))
    assert r.mw == 0.0
    assert r.status == "CALM"


def test_wind_above_cutout_is_zero(spec):
    r = simulate_wind_block(spec, make_block(10, ws100=30.0))
    assert r.mw == 0.0
    assert r.status == "CUTOUT"


def test_wind_rated_region_capped(spec):
    r = simulate_wind_block(spec, make_block(10, ws100=18.0))
    # Between rated and cut-out -> rated power minus losses, never exceeding AC.
    assert r.mw <= spec.wind_ac_mw + 1e-9
    assert r.mw > 0.0


def test_wind_follows_power_curve_monotonic(spec):
    speeds = [4, 6, 8, 10, 12]
    outputs = [simulate_wind_block(spec, make_block(10, ws100=s)).mw for s in speeds]
    # Output strictly increases with wind speed across the rising part of the curve.
    assert all(b > a for a, b in zip(outputs, outputs[1:], strict=False))


def test_wind_hub_extrapolation_from_10m(spec):
    # Only a 10 m/s reading at 10 m: power-law lifts it toward hub height (100 m).
    r = simulate_wind_block(spec, make_block(10, ws100=None, ws10=10.0))
    assert r.v_hub > 10.0


# ----------------------------- Hybrid -------------------------------------- #
def test_hybrid_totals_reconcile(spec, synthetic_day):
    results = simulate_day(spec, synthetic_day)
    assert len(results) == 96
    for r in results:
        assert abs(r.total_mw - (r.solar_mw + r.wind_mw)) < 1e-9
        assert abs(r.total_mwh - (r.solar_mwh + r.wind_mwh)) < 1e-9
        assert r.solar_mw <= spec.solar_ac_mw + 1e-9
        assert r.wind_mw <= spec.wind_ac_mw + 1e-9
        assert r.solar_mw >= 0 and r.wind_mw >= 0


def test_realism_texture_deterministic_and_bounded(spec, synthetic_day):
    import dataclasses

    from app.quality import check_day

    # Cloudy + gusty version of the synthetic day to activate texture.
    cloudy = [
        dataclasses.replace(b, cloud_cover=70.0, wind_gusts_10m=(b.wind_speed_100m or 0) * 1.4)
        for b in synthetic_day
    ]
    a = simulate_day(spec, cloudy, texture=True)
    b = simulate_day(spec, cloudy, texture=True)
    plain = simulate_day(spec, cloudy, texture=False)

    # Deterministic: identical across runs.
    assert [r.total_mw for r in a] == [r.total_mw for r in b]
    # Adds variability vs the smooth physics-only result.
    assert any(abs(x.total_mw - y.total_mw) > 1e-6 for x, y in zip(a, plain, strict=False))
    # Invariants still hold with texture on.
    report = check_day(spec, a)
    assert report.status in ("OK", "PARTIAL")
    assert not report.issues
    for r in a:
        assert r.solar_mw >= 0 and r.wind_mw >= 0
        assert r.solar_mw <= spec.solar_ac_mw + 1e-9
        assert r.wind_mw <= spec.wind_ac_mw + 1e-9
        if r.solar_status == "NIGHT":
            assert r.solar_mw == 0.0


def test_hybrid_solar_bell_shape(spec, synthetic_day):
    results = simulate_day(spec, synthetic_day)
    solar = [r.solar_mw for r in results]
    # Midnight zero, noon (block 48 ~ 11:45-12:00) is the peak region.
    assert solar[0] == 0.0
    assert solar[95] == 0.0
    assert max(solar) == max(solar[40:56])
