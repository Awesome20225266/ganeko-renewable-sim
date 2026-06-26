#!/usr/bin/env python3
"""Poll OUR wrapper's /api/renewable/current endpoint for real-time testing.

This talks to the wrapper API only (never the external provider directly), so the
external provider key never appears here. Configure via environment:

  WRAPPER_BASE_URL                 default http://localhost:8000
  RENEWABLE_WRAPPER_USER_API_KEY   sent as X-API-Key if set (the wrapper user key)

Usage:
  python scripts/poll_renewable_current.py
  python scripts/poll_renewable_current.py --poll-seconds 60
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

import httpx


def main() -> None:
    ap = argparse.ArgumentParser(description="Poll the renewable wrapper /current endpoint.")
    ap.add_argument("--poll-seconds", type=int, default=60, help="Polling interval (default 60).")
    ap.add_argument(
        "--base-url",
        default=os.environ.get("WRAPPER_BASE_URL", "http://localhost:8000"),
        help="Wrapper base URL (default http://localhost:8000 or $WRAPPER_BASE_URL).",
    )
    args = ap.parse_args()

    url = f"{args.base_url.rstrip('/')}/api/renewable/current"
    headers: dict[str, str] = {}
    user_key = os.environ.get("RENEWABLE_WRAPPER_USER_API_KEY")
    if user_key:
        headers["X-API-Key"] = user_key  # the WRAPPER user key, never the provider key

    interval = max(1, args.poll_seconds)
    last_block_start = None
    print(f"Polling {url} every {interval}s  (Ctrl+C to stop)")

    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            r = httpx.get(url, headers=headers, timeout=30.0)
        except Exception as exc:  # noqa: BLE001 — show a clean message, keep polling
            print(f"[{now}] request failed: {exc}")
            time.sleep(interval)
            continue

        if r.status_code != 200:
            detail = ""
            try:
                detail = r.json().get("detail", "")
            except Exception:  # noqa: BLE001
                pass
            print(f"[{now}] HTTP {r.status_code} {detail}".rstrip())
            time.sleep(interval)
            continue

        d = r.json()
        bs = d.get("block_start")
        if bs == last_block_start:
            print(f"[{now}] No new block yet. Latest block is {bs}. "
                  f"Checking again in {interval} seconds...")
        else:
            last_block_start = bs
            print(f"[{now}] NEW BLOCK")
            print(f"    plant_id   : {d.get('plant_id')}")
            print(f"    block_no   : {d.get('block_no')}")
            print(f"    block_start: {bs}")
            print(f"    solar_mw   : {d.get('solar_mw')}")
            print(f"    wind_mw    : {d.get('wind_mw')}")
            print(f"    total_mw   : {d.get('total_mw')}")
            print(f"    data_label : {d.get('data_label')}")
            print(f"    fetched_at : {now}")
        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)
