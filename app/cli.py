"""Command-line interface: seed / simulate / reprocess / backfill / scheduler jobs."""
from __future__ import annotations

import argparse
import time
from datetime import date, datetime, timedelta

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

    p_bf = sub.add_parser(
        "backfill",
        help="Fill a date range with simulations (default HISTORICAL); skips dates "
        "already present unless --force.",
    )
    p_bf.add_argument("--plant", default=None)
    p_bf.add_argument("--start", required=True, type=_parse_date, help="YYYY-MM-DD (inclusive)")
    p_bf.add_argument("--end", required=True, type=_parse_date, help="YYYY-MM-DD (inclusive)")
    p_bf.add_argument(
        "--mode", choices=["HISTORICAL", "LIVE", "FORECAST"], default="HISTORICAL"
    )
    p_bf.add_argument(
        "--force", action="store_true", help="Re-run dates that already have data"
    )
    p_bf.add_argument(
        "--sleep", type=float, default=0.0,
        help="Seconds to pause between dates (be gentle on the weather API)",
    )
    p_bf.add_argument(
        "--dry-run", action="store_true",
        help="List the dates that would be processed/skipped, then exit (no writes)",
    )

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

    if args.cmd == "backfill":
        from app.api.repository import get_present_dates
        from app.db.base import session_scope
        from app.simulate import run_simulation_sync

        plant = args.plant or default_plant
        if args.start > args.end:
            print("ERROR: --start must be on or before --end")
            return 2
        mode = DataMode(args.mode)
        # HISTORICAL is for completed past days only — never fabricate future history.
        if mode == DataMode.HISTORICAL:
            from zoneinfo import ZoneInfo

            tz = get_settings().PLANT_TZ
            try:
                today = datetime.now(ZoneInfo(tz)).date()
            except Exception:  # noqa: BLE001 — bad tz config shouldn't crash the guard
                today = datetime.utcnow().date()
            if args.end > today:
                print(
                    f"ERROR: --end {args.end} is in the future (today={today} in {tz}). "
                    "HISTORICAL backfill only covers completed dates."
                )
                return 2
        all_dates = [
            args.start + timedelta(days=i)
            for i in range((args.end - args.start).days + 1)
        ]
        if args.force:
            present: set[date] = set()
        else:
            with session_scope() as db:
                present = get_present_dates(db, plant, args.start, args.end, mode.value)
        todo = [d for d in all_dates if d not in present]
        skipped = [d for d in all_dates if d in present]

        print(
            f"Backfill {plant} {mode.value}: {args.start}..{args.end} "
            f"({len(all_dates)} days) -> {len(todo)} to process, "
            f"{len(skipped)} already present (skipped)"
        )
        if args.dry_run:
            if todo:
                print("Would process:", ", ".join(d.isoformat() for d in todo))
            if skipped:
                print("Would skip   :", ", ".join(d.isoformat() for d in skipped))
            return 0

        ok, failed = [], []
        for i, d in enumerate(todo, start=1):
            try:
                s = run_simulation_sync(
                    plant, d, mode, triggered_by="backfill", force_refetch=True,
                )
                ok.append(d)
                print(
                    f"  [{i}/{len(todo)}] {d} {s.data_label} {s.quality_status} "
                    f"total={s.total_mwh:.1f}MWh blocks={s.blocks_written}"
                )
            except Exception as exc:  # noqa: BLE001 — keep going; report at the end
                failed.append((d, str(exc)))
                print(f"  [{i}/{len(todo)}] {d} FAILED: {exc}")
            if args.sleep and i < len(todo):
                time.sleep(args.sleep)

        print(
            f"\nBackfill done: {len(ok)} ok, {len(skipped)} skipped, {len(failed)} failed."
        )
        if failed:
            print("Failed dates (re-run backfill to retry; succeeded dates are skipped):")
            for d, msg in failed:
                print(f"  {d}: {msg}")
            return 1
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
