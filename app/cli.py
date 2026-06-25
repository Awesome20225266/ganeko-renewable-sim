"""Command-line interface: seed / simulate / reprocess / scheduler jobs."""
from __future__ import annotations

import argparse
from datetime import date, datetime

from app.logging_conf import configure_logging
from app.weather.client import DataMode


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="renewable-sim", description="Simulation CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("seed", help="Idempotently seed plant + admin key")

    p_sim = sub.add_parser("simulate", help="Run a simulation for one date")
    p_sim.add_argument("--plant", default=None)
    p_sim.add_argument("--date", required=True, type=_parse_date)
    p_sim.add_argument("--mode", choices=["HISTORICAL", "LIVE", "FORECAST"], default=None)
    p_sim.add_argument("--force", action="store_true", help="Force refetch")

    p_re = sub.add_parser("reprocess", help="Reprocess one or more dates")
    p_re.add_argument("--plant", default=None)
    p_re.add_argument("--dates", nargs="+", required=True, type=_parse_date)
    p_re.add_argument("--mode", choices=["HISTORICAL", "LIVE", "FORECAST"], default=None)

    sub.add_parser("run-daily", help="Run the daily scheduler job once")
    sub.add_parser("live-refresh", help="Run the live-refresh job once")

    args = parser.parse_args(argv)

    from app.config.settings import get_settings

    default_plant = get_settings().PLANT_CODE

    if args.cmd == "seed":
        from app.db.seed import run_seed

        summary = run_seed()
        print("Seed:", summary.get("created") or "already seeded")
        if "admin_key" in summary:
            print("ADMIN KEY:", summary["admin_key"])
        return 0

    if args.cmd == "simulate":
        from app.simulate import run_simulation_sync

        mode = DataMode(args.mode) if args.mode else None
        s = run_simulation_sync(
            args.plant or default_plant, args.date, mode,
            triggered_by="manual", force_refetch=args.force,
        )
        print(
            f"{s.sim_date} mode={s.mode} label={s.data_label} quality={s.quality_status} "
            f"blocks={s.blocks_written} solar={s.solar_mwh:.1f}MWh wind={s.wind_mwh:.1f}MWh "
            f"total={s.total_mwh:.1f}MWh"
        )
        return 0

    if args.cmd == "reprocess":
        from app.simulate import run_simulation_sync

        mode = DataMode(args.mode) if args.mode else None
        for d in args.dates:
            s = run_simulation_sync(
                args.plant or default_plant, d, mode,
                triggered_by="reprocess", force_refetch=True,
            )
            print(f"reprocessed {d}: {s.data_label} {s.quality_status} total={s.total_mwh:.1f}MWh")
        return 0

    if args.cmd == "run-daily":
        from app.scheduler.service import run_daily_job

        run_daily_job()
        return 0

    if args.cmd == "live-refresh":
        from app.scheduler.service import run_live_refresh

        run_live_refresh()
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
