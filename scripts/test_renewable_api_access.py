"""Local terminal test: verify API key access to live, historical, and forecast data.

Windows PowerShell:

  py scripts/test_renewable_api_access.py
  py scripts/test_renewable_api_access.py --debug
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, timedelta
from typing import Any

BASE_URL = os.environ.get("RENEWABLE_API_BASE_URL", "https://renewable-sim.onrender.com")
PLANT_ID = os.environ.get("RENEWABLE_PLANT_ID", "HYBRID01")

# Read the key from the environment — never hardcode a key in the file.
#   PowerShell:  $env:RENEWABLE_API_KEY = "<your-key>"; py scripts/test_renewable_api_access.py
API_KEY = os.environ.get("RENEWABLE_API_KEY", "")

REQUEST_TIMEOUT = 30

EndpointResult = dict[str, Any]


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def access_label(status_code: int | None, *, forecast: bool = False) -> str:
    if status_code == 200:
        return "ALLOWED"
    if status_code in (401, 403):
        return "BLOCKED"
    # Forecast 404 usually means auth passed but no simulation exists yet.
    if forecast and status_code == 404:
        return "ALLOWED (no data)"
    if status_code is None:
        return "BLOCKED"
    return "BLOCKED"


def count_labels(blocks: list[dict]) -> Counter[str]:
    return Counter(block.get("data_label", "UNKNOWN") for block in blocks)


def request_json(path: str, *, debug: bool) -> EndpointResult:
    url = f"{BASE_URL}{path}"
    request = urllib.request.Request(
        url,
        headers={
            "X-API-Key": API_KEY,
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            body = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        if debug:
            traceback.print_exc()
        return {
            "ok": False,
            "status_code": exc.code,
            "error": detail or exc.reason,
            "data": None,
        }
    except urllib.error.URLError as exc:
        if debug:
            traceback.print_exc()
        return {
            "ok": False,
            "status_code": None,
            "error": f"Network error: {exc.reason}",
            "data": None,
        }
    except Exception as exc:
        if debug:
            traceback.print_exc()
        return {
            "ok": False,
            "status_code": None,
            "error": str(exc),
            "data": None,
        }

    try:
        data = json.loads(body) if body else None
    except json.JSONDecodeError as exc:
        if debug:
            traceback.print_exc()
        return {
            "ok": False,
            "status_code": status,
            "error": f"Invalid JSON: {exc}",
            "data": None,
        }

    return {
        "ok": status == 200,
        "status_code": status,
        "error": None,
        "data": data,
    }


def print_error(result: EndpointResult, *, show_status: bool = True) -> None:
    status = result["status_code"]
    error = result["error"] or "Unknown error"
    if show_status:
        print(f"Status code : {status if status is not None else 'N/A'}")
    print(f"Error       : {error}")


def test_current(debug: bool) -> EndpointResult:
    section("1. CURRENT / LIVE LATEST")
    result = request_json(f"/plants/{PLANT_ID}/current", debug=debug)
    status = result["status_code"]
    print(f"Status code : {status if status is not None else 'N/A'}")

    if not result["ok"]:
        print_error(result)
        return result

    data = result["data"] or {}
    print(f"Block Start : {data.get('block_start', '-')}")
    print(f"Block No    : {data.get('block_no', '-')}")
    print(f"Solar MW    : {data.get('solar_mw', '-')}")
    print(f"Wind MW     : {data.get('wind_mw', '-')}")
    print(f"Total MW    : {data.get('total_mw', '-')}")
    print(f"Data Label  : {data.get('data_label', '-')}")
    return result


def test_live(debug: bool) -> tuple[EndpointResult, int]:
    section("2. TODAY FULL LIVE CURVE")
    result = request_json(f"/plants/{PLANT_ID}/live", debug=debug)
    status = result["status_code"]
    print(f"Status code : {status if status is not None else 'N/A'}")

    if not result["ok"]:
        print_error(result)
        return result, 0

    data = result["data"] or {}
    blocks = data.get("blocks") or []
    labels = count_labels(blocks)
    live_count = labels.get("LIVE_ESTIMATED", 0)
    forecast_count = labels.get("FORECAST_SIMULATED", 0)
    historical_count = labels.get("HISTORICAL_SIMULATED", 0)

    print(f"Total blocks              : {len(blocks)}")
    print(f"Current block no          : {data.get('current_block_no', '-')}")
    print(f"LIVE_ESTIMATED blocks     : {live_count}")
    print(f"FORECAST_SIMULATED blocks: {forecast_count}")
    print(f"HISTORICAL_SIMULATED blocks: {historical_count}")

    if forecast_count > 0:
        print(
            "Conclusion: WARNING: /live endpoint exposes forecast blocks also."
        )
    else:
        print(
            "Conclusion: /live endpoint did not expose forecast blocks in this response."
        )

    return result, forecast_count


def test_historical(debug: bool) -> EndpointResult:
    section("3. HISTORICAL DATA")
    yesterday = date.today() - timedelta(days=1)
    date_str = yesterday.isoformat()
    result = request_json(
        f"/plants/{PLANT_ID}/historical?date={date_str}",
        debug=debug,
    )
    status = result["status_code"]
    print(f"Status code : {status if status is not None else 'N/A'}")
    print(f"Date used   : {date_str}")

    if not result["ok"]:
        print_error(result)
        return result

    data = result["data"] or {}
    blocks = data.get("blocks") or []
    labels = count_labels(blocks)

    print(f"Number of blocks : {len(blocks)}")
    print("Count by data_label:")
    for label, count in sorted(labels.items()):
        print(f"  {label}: {count}")

    if blocks:
        print(f"First block_start : {blocks[0].get('block_start', '-')}")
        print(f"Last block_end    : {blocks[-1].get('block_end', '-')}")
        print(f"Sample total_mw   : {blocks[0].get('total_mw', '-')}")
    else:
        print("First block_start : -")
        print("Last block_end    : -")
        print("Sample total_mw   : -")

    if labels.get("FORECAST_SIMULATED", 0) == 0:
        print(
            "Conclusion: Historical data access is working and does not appear forecasted."
        )
    else:
        print(
            "Conclusion: Historical response includes FORECAST_SIMULATED labels."
        )

    return result


def test_forecast_horizon(debug: bool) -> EndpointResult:
    section("4. FORECAST BY HORIZON")
    result = request_json(
        f"/plants/{PLANT_ID}/forecast?horizon_days=1",
        debug=debug,
    )
    status = result["status_code"]
    print(f"Status code : {status if status is not None else 'N/A'}")

    if status == 200 and result["data"] is not None:
        data = result["data"]
        blocks = data.get("blocks") or []
        labels = count_labels(blocks)
        print(f"Blocks returned           : {len(blocks)}")
        print(f"FORECAST_SIMULATED blocks : {labels.get('FORECAST_SIMULATED', 0)}")
        print(f"Data labels found         : {', '.join(sorted(labels)) or '-'}")
        print("Conclusion: FORECAST ACCESS IS ALLOWED with this API key.")
    elif status in (401, 403):
        print_error(result, show_status=False)
        print(
            "Conclusion: FORECAST ACCESS IS BLOCKED or unauthorized with this API key."
        )
    elif status == 404:
        print_error(result, show_status=False)
        print(
            "Conclusion: Forecast endpoint is reachable with this key, but no "
            "FORECAST simulation exists for the requested date yet."
        )
    else:
        print_error(result, show_status=False)
        print("Conclusion: Forecast horizon request failed with an unexpected error.")

    return result


def test_forecast_date(debug: bool) -> EndpointResult:
    section("5. FORECAST BY FUTURE DATE")
    tomorrow = date.today() + timedelta(days=1)
    date_str = tomorrow.isoformat()
    result = request_json(
        f"/plants/{PLANT_ID}/forecast?date={date_str}",
        debug=debug,
    )
    status = result["status_code"]
    print(f"Status code : {status if status is not None else 'N/A'}")
    print(f"Date used   : {date_str}")

    if status == 200 and result["data"] is not None:
        blocks = result["data"].get("blocks") or []
        labels = count_labels(blocks)
        print("Data label counts:")
        for label, count in sorted(labels.items()):
            print(f"  {label}: {count}")
        print("Conclusion: Forecast by future date is accessible.")
    elif status in (401, 403):
        print_error(result, show_status=False)
        print("Conclusion: Forecast by future date is blocked.")
    elif status == 404:
        print_error(result, show_status=False)
        print(
            "Conclusion: Forecast endpoint is reachable with this key, but no "
            "simulation exists for the requested future date yet."
        )
    else:
        print_error(result, show_status=False)
        print("Conclusion: Forecast by future date request failed unexpectedly.")

    return result


def print_summary(
    current: EndpointResult,
    live: EndpointResult,
    historical: EndpointResult,
    forecast_horizon: EndpointResult,
    forecast_date: EndpointResult,
    live_forecast_block_count: int,
) -> None:
    section("FINAL SUMMARY")

    rows = [
        ("/current", current["status_code"], False),
        ("/live", live["status_code"], False),
        ("/historical", historical["status_code"], False),
        ("/forecast?horizon_days=1", forecast_horizon["status_code"], True),
        ("/forecast?date=tomorrow", forecast_date["status_code"], True),
    ]

    print(f"{'Endpoint':<28} Result")
    print("-" * 44)
    for endpoint, status_code, is_forecast in rows:
        print(f"{endpoint:<28} {access_label(status_code, forecast=is_forecast)}")

    print()
    print("Final conclusion:")
    print()

    horizon_status = forecast_horizon["status_code"]
    date_status = forecast_date["status_code"]
    forecast_allowed = horizon_status in (200, 404) or date_status in (200, 404)
    forecast_blocked = horizon_status in (401, 403) and date_status in (401, 403)

    if horizon_status == 200 or date_status == 200:
        print(
            "Case A: This API key CAN fetch forecasted data. If users should only get "
            "live/historical data, do not share this key directly. Build a backend "
            "wrapper that blocks /forecast and filters FORECAST_SIMULATED blocks."
        )
    elif forecast_blocked:
        print(
            "Case B: This API key appears restricted from forecast data and can only "
            "access allowed endpoints."
        )
    elif forecast_allowed:
        print(
            "Case A (partial): Forecast endpoints are reachable with this key, but no "
            "forecast simulation data was returned for the tested dates. Re-run after "
            "forecast data is generated, or use a backend wrapper if users should not "
            "access /forecast directly."
        )
    else:
        print(
            "Review the forecast section errors above. Forecast access could not be "
            "confirmed cleanly."
        )

    if live_forecast_block_count > 0:
        print()
        print(
            "Case C: Even if /forecast is blocked, /live may expose future forecast blocks. "
            "A wrapper should filter /live and return only completed LIVE_ESTIMATED blocks."
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test Renewable Generation API key access for HYBRID01."
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print stack traces on unexpected errors.",
    )
    args = parser.parse_args()

    if not API_KEY:
        print("ERROR: set RENEWABLE_API_KEY in your environment first "
              '(PowerShell: $env:RENEWABLE_API_KEY = "<your-key>").', file=sys.stderr)
        return 1

    print("Renewable Generation API Access Test")
    print(f"Base URL : {BASE_URL}")
    print(f"Plant ID : {PLANT_ID}")

    current = test_current(args.debug)
    live, live_forecast_count = test_live(args.debug)
    historical = test_historical(args.debug)
    forecast_horizon = test_forecast_horizon(args.debug)
    forecast_date = test_forecast_date(args.debug)

    print_summary(
        current,
        live,
        historical,
        forecast_horizon,
        forecast_date,
        live_forecast_count,
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
