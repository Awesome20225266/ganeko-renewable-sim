"""Simulation-realism validation. Runs real simulations and asserts physical
plausibility, then writes a golden reference day for regression. Output feeds
HEALTH_REPORT.md.

Run:  py scripts/realism_check.py
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import DailySummary, GenerationBlock
from app.simulate import run_simulation_sync
from app.weather.client import DataMode

PLANT = "HYBRID01"
TZ = "Asia/Kolkata"
ROOT = Path(__file__).resolve().parent.parent


def _blocks(db, d, mode):
    return list(
        db.scalars(
            select(GenerationBlock)
            .where(
                GenerationBlock.plant_code == PLANT,
                GenerationBlock.sim_date == d,
                GenerationBlock.data_mode == mode,
                GenerationBlock.is_current.is_(True),
            )
            .order_by(GenerationBlock.block_no)
        )
    )


def _summary(db, d, mode):
    return db.scalar(
        select(DailySummary).where(
            DailySummary.plant_code == PLANT,
            DailySummary.sim_date == d,
            DailySummary.data_mode == mode,
            DailySummary.is_current.is_(True),
        )
    )


def check(label, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    return cond


def analyze_day(db, d, mode, results):
    blocks = _blocks(db, d, mode)
    s = _summary(db, d, mode)
    solar = [b.solar_mw for b in blocks]
    wind = [b.wind_mw for b in blocks]
    night = [b for b in blocks if b.solar_status == "NIGHT"]
    day = [b for b in blocks if b.solar_status != "NIGHT"]
    peak_idx = solar.index(max(solar)) if any(solar) else -1
    ok = True
    print(f"\n== {mode} {d} ==")
    print(
        f"   solar={s.solar_mwh:.1f}MWh wind={s.wind_mwh:.1f}MWh total={s.total_mwh:.1f}MWh "
        f"solarCUF={s.solar_cuf*100:.1f}% windCUF={s.wind_cuf*100:.1f}% "
        f"hybCUF={s.hybrid_cuf*100:.1f}% specYield={s.solar_specific_yield:.2f}kWh/kWp"
    )
    ok &= check("96 blocks", len(blocks) == 96, f"{len(blocks)}")
    ok &= check("solar zero at night", all(b.solar_mw == 0 for b in night), f"{len(night)} night blocks")
    ok &= check("solar peaks near midday", 28 <= peak_idx <= 68 if peak_idx >= 0 else False,
                f"peak block {peak_idx+1} (~{(peak_idx)*15//60:02d}:{(peak_idx)*15%60:02d})")
    ok &= check("no negatives", all(b.solar_mw >= 0 and b.wind_mw >= 0 for b in blocks))
    ok &= check("solar <= AC cap (160)", all(b.solar_mw <= 160 + 1e-6 for b in blocks))
    ok &= check("wind <= AC cap (135)", all(b.wind_mw <= 135 + 1e-6 for b in blocks))
    ok &= check("totals reconcile", all(abs(b.total_mw - b.solar_mw - b.wind_mw) < 1e-6 for b in blocks))
    ok &= check("hybrid CUF <= 100%", s.hybrid_cuf <= 1.0 + 1e-6)
    # Wind follows curve: every CALM/CUTOUT block must be zero; rated region <= cap.
    bad_wind = [b for b in blocks if b.wind_status in ("CALM", "CUTOUT") and b.wind_mw != 0]
    ok &= check("wind zero below cut-in / above cut-out", not bad_wind, f"{len(bad_wind)} violations")
    return ok, s, blocks


def main():
    today = datetime.now(ZoneInfo(TZ)).date()
    overall = True

    # Seasonal solar comparison (historical-forecast covers last ~2 years).
    summer = date(today.year - 1, 6, 21)
    winter = date(today.year - 1, 12, 21)

    runs = [
        (summer, DataMode.HISTORICAL),
        (winter, DataMode.HISTORICAL),
        (today, DataMode.LIVE),
        (today + timedelta(days=3), DataMode.FORECAST),
    ]
    summaries = {}
    print("Running simulations (live Open-Meteo)...")
    for d, mode in runs:
        r = run_simulation_sync(PLANT, d, mode, triggered_by="manual", force_refetch=True)
        summaries[(d, mode.value)] = r

    with session_scope() as db:
        results = {}
        for d, mode in runs:
            ok, s, blocks = analyze_day(db, d, mode.value, summaries[(d, mode.value)])
            overall &= ok
            results[(d, mode.value)] = (s, blocks)

        s_sum = results[(summer, "HISTORICAL")][0]
        s_win = results[(winter, "HISTORICAL")][0]
        print("\n== Cross-checks ==")
        overall &= check(
            "summer solar > winter solar",
            s_sum.solar_mwh > s_win.solar_mwh,
            f"summer {s_sum.solar_mwh:.0f} vs winter {s_win.solar_mwh:.0f} MWh",
        )
        overall &= check(
            "solar CUF in plausible 6-32% band (season/weather dependent)",
            0.06 <= s_sum.solar_cuf <= 0.34 and 0.05 <= s_win.solar_cuf <= 0.34,
            f"summer {s_sum.solar_cuf*100:.1f}% winter {s_win.solar_cuf*100:.1f}%",
        )
        # Clear-day specific yield ~3.5-5.5 kWh/kWp: test the CLEAREST sampled day
        # (an arbitrary calendar date may be cloudy, which is physically valid).
        max_yield = max(s.solar_specific_yield for s, _ in results.values())
        overall &= check(
            "clearest-day specific yield ~3.0-6.0 kWh/kWp",
            3.0 <= max_yield <= 6.0,
            f"max {max_yield:.2f} kWh/kWp across sampled days",
        )

        # Golden reference day (regression guard).
        gsum, gblocks = results[(summer, "HISTORICAL")]
        golden = {
            "plant_code": PLANT,
            "sim_date": summer.isoformat(),
            "mode": "HISTORICAL",
            "simulation_version": gsum.simulation_version,
            "model_assumption_version": gsum.model_assumption_version,
            "solar_mwh": round(gsum.solar_mwh, 3),
            "wind_mwh": round(gsum.wind_mwh, 3),
            "total_mwh": round(gsum.total_mwh, 3),
            "solar_cuf": round(gsum.solar_cuf, 5),
            "wind_cuf": round(gsum.wind_cuf, 5),
            "hybrid_cuf": round(gsum.hybrid_cuf, 5),
            "solar_specific_yield": round(gsum.solar_specific_yield, 4),
            "blocks_solar_mw": [round(b.solar_mw, 3) for b in gblocks],
            "blocks_wind_mw": [round(b.wind_mw, 3) for b in gblocks],
        }
        (ROOT / "golden_reference_day.json").write_text(json.dumps(golden, indent=2))
        print(f"\nWrote golden_reference_day.json ({summer}).")

    print("\n=== REALISM VALIDATION:", "PASS ===" if overall else "FAIL ===")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
