"""Day-ahead P90 schedule engine.

Produces a 96-block schedule anchored on the simulated generation, carrying a
realistic day-ahead forecast error rather than a flat percentage band.

Why the error is deliberately NON-uniform: real forecast error is not a constant
percentage of output. It is tight through a clear midday, wide on the sunrise /
sunset ramps where a few minutes of timing error is a large miss, and widest
during cloud or wind episodes. A flat band is the giveaway of synthetic data, so
sigma is built from five layers:

  1. weather-driven base  - solar scales with that block's cloud cover; wind
                            scales with the POWER-CURVE SLOPE, so it is near-zero
                            below cut-in and above rated (flat curve) and peaks
                            in the steep 6-11 m/s region
  2. ramp / timing        - proportional to |d(MW)/dt|
  3. afternoon convection - widens into the 12:00-16:30 window
  4. slow regime          - AR(1), ~2h correlation: forecast quality comes in
                            good and bad stretches, it does not reset each block
  5. episodes             - 1-2 discrete busts per day

The error is applied as a MEAN-PRESERVING LOGNORMAL, not an additive Gaussian.
An additive draw followed by clip(0) piles probability mass exactly on zero, so
on the ~1% of days where the AR(1) takes a sustained negative excursion the
schedule flatlines at 0 MW for hours while the plant is genuinely running. The
lognormal is strictly positive and scales with output, which is also the right
shape for forecast error at low generation.

Deterministic: seeded from (plant, date, component), so a given date always
yields the same schedule. Same philosophy as engines/texture.py.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np

from app.engines.hybrid import BlockResult
from app.engines.spec import PlantSpec
from app.weather.normalize import NormalizedBlock

# z for a 90% one-sided interval — the band half-width, not a shift of the line.
Z90 = 1.2816


@dataclass
class ScheduleParams:
    """Tunable error model. Surfaced through settings so it needs no code change."""

    sigma_scale: float = 1.15       # master accuracy knob; 1.15 -> ~10% MAPE
    rel_sigma_cap: float = 0.32     # bounds the worst sustained relative miss
    day_energy_cap: float = 0.05    # max |day MWh deviation| vs actual
    ar1_phi: float = 0.80
    bias_sigma: float = 0.015


@dataclass
class ScheduleBlock:
    block_no: int
    block_start: datetime
    block_end: datetime
    solar_p90_mw: float
    wind_p90_mw: float
    total_p90_mw: float
    solar_p90_mwh: float
    wind_p90_mwh: float
    total_p90_mwh: float
    solar_band_low_mw: float
    solar_band_high_mw: float
    wind_band_low_mw: float
    wind_band_high_mw: float
    total_band_low_mw: float
    total_band_high_mw: float
    solar_sigma_mw: float
    wind_sigma_mw: float


# --------------------------------------------------------------------------- #
# Deterministic randomness
# --------------------------------------------------------------------------- #
def _rng(*parts) -> np.random.Generator:
    seed = int(hashlib.md5("|".join(map(str, parts)).encode("utf-8")).hexdigest()[:8], 16)
    return np.random.default_rng(seed)


def _ar1(rng: np.random.Generator, n: int, phi: float) -> np.ndarray:
    """Unit-variance AR(1) — errors drift over hours instead of jittering."""
    e = rng.standard_normal(n)
    out = np.zeros(n)
    out[0] = e[0]
    root = np.sqrt(1.0 - phi**2)
    for i in range(1, n):
        out[i] = phi * out[i - 1] + root * e[i]
    return out


def _regime(rng: np.random.Generator, n: int) -> np.ndarray:
    """Slow-varying multiplier: good and bad forecast stretches within a day."""
    return np.exp(0.30 * _ar1(rng, n, 0.94))


def _episodes(rng: np.random.Generator, n: int) -> np.ndarray:
    """1-2 short busts per day (cloud band early, wind ramp mistimed)."""
    mult = np.ones(n)
    for _ in range(int(rng.integers(1, 3))):
        start = int(rng.integers(8, max(9, n - 10)))
        length = int(rng.integers(2, 6))
        peak = float(rng.uniform(1.9, 3.1))
        for j in range(length):
            if start + j < n:
                shape = np.sin(np.pi * (j + 0.5) / length)  # ease in/out
                mult[start + j] = max(mult[start + j], 1.0 + (peak - 1.0) * shape)
    return mult


# --------------------------------------------------------------------------- #
# Per-component sigma
# --------------------------------------------------------------------------- #
def _sigma_solar(spec: PlantSpec, actual: np.ndarray, cloud: np.ndarray) -> np.ndarray:
    n = len(actual)
    hour = np.arange(n) * (spec.block_minutes / 60.0)

    # 1. output-proportional, amplified by cloud cover
    k_out = 0.030 + 0.135 * (np.clip(cloud, 0.0, 100.0) / 100.0) ** 1.3
    sigma = k_out * actual

    # 2. ramp/timing — a ~7 min shift on the shoulder is a large miss
    sigma = sigma + 0.28 * np.abs(np.diff(actual, prepend=actual[0]))

    # 3. small capacity floor while the sun is up
    sigma = sigma + 0.006 * spec.solar_ac_mw * (actual > 0.5)

    # 4. afternoon convective development
    aft = np.clip((hour - 11.5) / 4.0, 0, 1) * np.clip((18.5 - hour) / 2.0, 0, 1)
    sigma = sigma * (1.0 + 0.40 * aft)

    sigma[actual <= 0.0] = 0.0  # night: no generation, no error
    return sigma


def _curve_slope(spec: PlantSpec, v: np.ndarray) -> np.ndarray:
    """dP/dv (MW per m/s) from the plant's own power curve.

    This is what makes wind sigma physical: the same 1 m/s speed error costs a
    lot of MW in the steep 6-11 m/s region and almost nothing above rated.
    """
    curve = sorted(spec.wind_power_curve, key=lambda p: p[0])
    speeds = np.array([p[0] for p in curve], dtype=float)
    powers_mw = np.array([p[1] for p in curve], dtype=float) / 1000.0
    return np.interp(v, speeds, np.gradient(powers_mw, speeds))


def _sigma_wind(spec: PlantSpec, actual: np.ndarray, wspd: np.ndarray) -> np.ndarray:
    n = len(actual)
    hour = np.arange(n) * (spec.block_minutes / 60.0)

    # 1. power-curve slope against a ~0.8 m/s day-ahead speed error
    sigma = 0.135 * _curve_slope(spec, wspd) * 0.8

    # 2. output-proportional + capacity floor
    sigma = sigma + 0.045 * actual + 0.006 * spec.wind_ac_mw

    # 3. evening / nocturnal transition is harder to time
    sigma = sigma * (1.0 + 0.25 * np.clip(np.sin(np.pi * (hour - 15.0) / 12.0), 0, 1))
    return np.maximum(sigma, 0.0)


# --------------------------------------------------------------------------- #
# Schedule construction
# --------------------------------------------------------------------------- #
def _component(
    plant_code: str,
    sim_date: date,
    name: str,
    actual: np.ndarray,
    sigma: np.ndarray,
    cap: float,
    params: ScheduleParams,
    night_mask: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    n = len(actual)
    sigma = sigma * params.sigma_scale
    sigma = sigma * _regime(_rng(plant_code, sim_date, name, "reg"), n)
    sigma = sigma * _episodes(_rng(plant_code, sim_date, name, "ev"), n)

    r = _rng(plant_code, sim_date, name)
    bias = float(np.clip(r.normal(0.0, params.bias_sigma), -0.035, 0.035))
    # Bounded tail: a 3-sigma sustained excursion is beyond what we want to model.
    eps = np.clip(_ar1(r, n, params.ar1_phi), -2.2, 2.2)

    # Mean-preserving lognormal (see module docstring for why not additive).
    floor = 0.02 * cap
    rel = np.minimum(sigma / np.maximum(actual, floor), params.rel_sigma_cap)
    sched = np.abs(actual * np.exp(bias + rel * eps - 0.5 * rel**2))

    # Submitted schedules are smooth.
    sched = np.convolve(sched, np.array([0.25, 0.5, 0.25]), mode="same")
    sched = np.clip(sched, 0.0, cap)
    if night_mask is not None:
        sched[night_mask] = 0.0

    # Day-energy calibration: a desk checks the day total before submitting, so an
    # unbounded random walk in day MWh is unrealistic. Preserves intra-day shape.
    act_sum, sch_sum = float(actual.sum()), float(sched.sum())
    if act_sum > 1e-6 and sch_sum > 1e-6:
        dev = sch_sum / act_sum - 1.0
        if abs(dev) > params.day_energy_cap:
            sched = sched * (1.0 + np.sign(dev) * params.day_energy_cap) / (1.0 + dev)
            sched = np.clip(sched, 0.0, cap)
            if night_mask is not None:
                sched[night_mask] = 0.0
    return sched, sigma


def build_schedule(
    spec: PlantSpec,
    results: list[BlockResult],
    weather: list[NormalizedBlock],
    sim_date: date,
    params: ScheduleParams | None = None,
) -> list[ScheduleBlock]:
    """Build the 96-block day-ahead P90 schedule for one plant/date.

    `results` is the simulated generation the schedule is anchored on; `weather`
    supplies cloud cover and hub-level wind speed that drive the uncertainty.
    """
    params = params or ScheduleParams()
    n = len(results)
    if n == 0:
        return []

    solar_a = np.array([r.solar_mw for r in results], dtype=float)
    wind_a = np.array([r.wind_mw for r in results], dtype=float)

    wx = {w.block_no: w for w in weather}
    cloud = np.array(
        [(wx[r.block_no].cloud_cover if r.block_no in wx and wx[r.block_no].cloud_cover
          is not None else 30.0) for r in results], dtype=float)
    wspd = np.array(
        [(wx[r.block_no].wind_speed_100m if r.block_no in wx and wx[r.block_no].wind_speed_100m
          is not None else 7.0) for r in results], dtype=float)

    night = solar_a <= 0.0
    solar_s, solar_sig = _component(
        spec.plant_code, sim_date, "solar", solar_a,
        _sigma_solar(spec, solar_a, cloud), spec.solar_ac_mw, params, night)
    wind_s, wind_sig = _component(
        spec.plant_code, sim_date, "wind", wind_a,
        _sigma_wind(spec, wind_a, wspd), spec.wind_ac_mw, params)

    total_s = solar_s + wind_s
    # Solar and wind errors are largely independent, so the hybrid band is
    # narrower than the sum of the parts — adding them would overstate it.
    total_sig = np.sqrt(solar_sig**2 + wind_sig**2)
    total_cap = spec.solar_ac_mw + spec.wind_ac_mw
    hours = spec.block_minutes / 60.0

    out: list[ScheduleBlock] = []
    for i, r in enumerate(results):
        out.append(
            ScheduleBlock(
                block_no=r.block_no,
                block_start=r.block_start,
                block_end=r.block_end,
                solar_p90_mw=float(solar_s[i]),
                wind_p90_mw=float(wind_s[i]),
                total_p90_mw=float(total_s[i]),
                solar_p90_mwh=float(solar_s[i] * hours),
                wind_p90_mwh=float(wind_s[i] * hours),
                total_p90_mwh=float(total_s[i] * hours),
                solar_band_low_mw=float(max(0.0, solar_s[i] - Z90 * solar_sig[i])),
                solar_band_high_mw=float(min(spec.solar_ac_mw, solar_s[i] + Z90 * solar_sig[i])),
                wind_band_low_mw=float(max(0.0, wind_s[i] - Z90 * wind_sig[i])),
                wind_band_high_mw=float(min(spec.wind_ac_mw, wind_s[i] + Z90 * wind_sig[i])),
                total_band_low_mw=float(max(0.0, total_s[i] - Z90 * total_sig[i])),
                total_band_high_mw=float(min(total_cap, total_s[i] + Z90 * total_sig[i])),
                solar_sigma_mw=float(solar_sig[i]),
                wind_sigma_mw=float(wind_sig[i]),
            )
        )
    return out
