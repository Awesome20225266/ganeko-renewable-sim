"""User-facing restricted API: live + historical only, never forecast.

Three layers block forecast data:
  1. No /forecast route exists and future dates are rejected at validation.
  2. Any block/summary with data_label == FORECAST_SIMULATED is stripped.
  3. /current returns 403 if its reading is a forecast.
Responses are clean, flat JSON (or Excel-friendly CSV) — no provider internals,
paths, secrets, or stack traces.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.config.settings import get_settings
from app.logging_conf import get_logger
from app.wrapper import client as provider
from app.wrapper.auth import require_wrapper_user

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/renewable",
    tags=["renewable-wrapper"],
    dependencies=[Depends(require_wrapper_user)],
)

DATA_POLICY = "LIVE_AND_HISTORICAL_ONLY_NO_FORECAST"
BLOCKED = provider.BLOCKED_LABEL
MAX_RANGE_DAYS = 31

# Flat, Excel-friendly columns (no nested provider objects).
BLOCK_COLUMNS = [
    "sim_date", "block_no", "block_start", "block_end",
    "solar_mw", "wind_mw", "total_mw",
    "solar_mwh", "wind_mwh", "total_mwh", "data_label",
]
SUMMARY_COLUMNS = [
    "sim_date", "data_label", "solar_mwh", "wind_mwh", "total_mwh",
    "solar_peak_mw", "wind_peak_mw", "solar_cuf", "wind_cuf",
    "hybrid_cuf", "solar_specific_yield",
]


# --- helpers -----------------------------------------------------------------
def _plant() -> str:
    return get_settings().RENEWABLE_PLANT_ID


def _today() -> date:
    """'Today' in the plant's timezone, so same-day requests aren't wrongly rejected."""
    try:
        return datetime.now(ZoneInfo(get_settings().RENEWABLE_PLANT_TZ)).date()
    except Exception:  # noqa: BLE001 — bad tz config shouldn't 500 a read
        return datetime.utcnow().date()


def _parse_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise HTTPException(400, f"Invalid {field} '{value}'. Use YYYY-MM-DD.") from None


def _guard(fn, *args, **kwargs):
    """Call the provider client and translate its errors into clean HTTP responses."""
    try:
        return fn(*args, **kwargs)
    except provider.ProviderNotConfigured:
        # Configuration problem on our side — clear, but leaks nothing.
        raise HTTPException(500, "Server is not configured to reach the renewable data provider.") from None
    except provider.ProviderError as e:
        raise HTTPException(e.status_code, e.message) from None


def _clean_block(b: dict, sim_date: str | None) -> dict:
    return {
        "sim_date": sim_date,
        "block_no": b.get("block_no"),
        "block_start": b.get("block_start"),
        "block_end": b.get("block_end"),
        "solar_mw": b.get("solar_mw"),
        "wind_mw": b.get("wind_mw"),
        "total_mw": b.get("total_mw"),
        "solar_mwh": b.get("solar_mwh"),
        "wind_mwh": b.get("wind_mwh"),
        "total_mwh": b.get("total_mwh"),
        "data_label": b.get("data_label"),
    }


def _csv(rows: list[dict], columns: list[str], filename: str) -> Response:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c) for c in columns})
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- 1) real-time latest reading --------------------------------------------
@router.get("/current")
def current(fmt: str = Query("json", alias="format", pattern="^(json|csv)$")):
    """Latest live-estimated reading for polling. 403 if the provider returns forecast."""
    plant = _plant()
    d = _guard(provider.fetch_current, plant)
    label = d.get("data_label")
    if label == BLOCKED:
        raise HTTPException(403, "Forecast data is not allowed.")
    clean = {
        "plant_id": plant,
        "block_no": d.get("block_no"),
        "block_start": d.get("block_start"),
        "block_end": d.get("block_end"),
        "solar_mw": d.get("solar_mw"),
        "wind_mw": d.get("wind_mw"),
        "total_mw": d.get("total_mw"),
        "energy_today_mwh": d.get("energy_today_mwh"),
        "hybrid_cuf": d.get("hybrid_cuf"),
        "data_label": label,
        "as_of": d.get("as_of"),
        "refresh_interval_minutes": d.get("refresh_interval_minutes"),
    }
    if fmt == "csv":
        return _csv([clean], list(clean.keys()), f"{plant}-current.csv")
    return clean


# --- 2) today's completed (live) blocks only --------------------------------
@router.get("/today-completed-blocks")
def today_completed_blocks(fmt: str = Query("json", alias="format", pattern="^(json|csv)$")):
    """Today's completed LIVE blocks only — all future/forecast blocks removed."""
    plant = _plant()
    d = _guard(provider.fetch_live, plant)
    current_block_no = d.get("current_block_no")
    sim_date = d.get("sim_date")
    blocks = []
    for b in d.get("blocks") or []:
        if b.get("data_label") != "LIVE_ESTIMATED":
            continue  # drops FORECAST_SIMULATED and anything not live-completed
        if current_block_no is not None and b.get("block_no") is not None and b["block_no"] > current_block_no:
            continue  # belt-and-suspenders: never return a future block
        blocks.append(_clean_block(b, sim_date))
    if fmt == "csv":
        return _csv(blocks, BLOCK_COLUMNS, f"{plant}-today-completed.csv")
    return {
        "plant_id": plant,
        "current_block_no": current_block_no,
        "data_policy": DATA_POLICY,
        "blocks": blocks,
    }


# --- 3) historical day -------------------------------------------------------
@router.get("/historical")
def historical(
    date_str: str = Query(..., alias="date", description="YYYY-MM-DD (not in the future)"),
    fmt: str = Query("json", alias="format", pattern="^(json|csv)$"),
):
    """Completed historical day. Future dates rejected; forecast blocks stripped."""
    plant = _plant()
    d = _parse_date(date_str, "date")
    if d > _today():
        raise HTTPException(400, "Future dates are not allowed for historical data.")
    data = _guard(provider.fetch_historical, plant, d.isoformat())
    sim_date = data.get("sim_date") or d.isoformat()
    blocks = [
        _clean_block(b, sim_date)
        for b in (data.get("blocks") or [])
        if b.get("data_label") != BLOCKED
    ]
    if fmt == "csv":
        return _csv(blocks, BLOCK_COLUMNS, f"{plant}-historical-{d.isoformat()}.csv")
    return {"plant_id": plant, "date": d.isoformat(), "data_policy": DATA_POLICY, "blocks": blocks}


# --- 4) historical block range ----------------------------------------------
@router.get("/range")
def block_range(
    start: str = Query(..., description="YYYY-MM-DD"),
    end: str = Query(..., description="YYYY-MM-DD"),
    fmt: str = Query("json", alias="format", pattern="^(json|csv)$"),
):
    """Historical 15-min blocks across a date range (max 31 days). No forecast."""
    plant = _plant()
    s = _parse_date(start, "start")
    e = _parse_date(end, "end")
    today = _today()
    if s > e:
        raise HTTPException(400, "start must be on or before end.")
    if s > today or e > today:
        raise HTTPException(400, "Future dates are not allowed.")
    if (e - s).days > MAX_RANGE_DAYS:
        raise HTTPException(400, f"Range too large; maximum {MAX_RANGE_DAYS} days.")
    data = _guard(provider.fetch_range, plant, s.isoformat(), e.isoformat())
    rows = []
    for day in data or []:
        sim_date = day.get("sim_date")
        for b in day.get("blocks") or []:
            if b.get("data_label") == BLOCKED:
                continue
            rows.append(_clean_block(b, sim_date))
    if fmt == "csv":
        return _csv(rows, BLOCK_COLUMNS, f"{plant}-range-{s.isoformat()}_{e.isoformat()}.csv")
    return {
        "plant_id": plant, "start": s.isoformat(), "end": e.isoformat(),
        "data_policy": DATA_POLICY, "blocks": rows,
    }


# --- 5/6) daily summary (single date or range) ------------------------------
@router.get("/summary")
def summary(
    date_str: str | None = Query(None, alias="date"),
    start: str | None = Query(None),
    end: str | None = Query(None),
    fmt: str = Query("json", alias="format", pattern="^(json|csv)$"),
):
    """Daily totals for completed dates. Future dates rejected; forecast stripped."""
    plant = _plant()
    today = _today()
    if start or end:
        if not (start and end):
            raise HTTPException(400, "Both start and end are required for a range summary.")
        s = _parse_date(start, "start")
        e = _parse_date(end, "end")
        if s > e:
            raise HTTPException(400, "start must be on or before end.")
        if s > today or e > today:
            raise HTTPException(400, "Future dates are not allowed.")
        params = {"start": s.isoformat(), "end": e.isoformat()}
    elif date_str:
        dd = _parse_date(date_str, "date")
        if dd > today:
            raise HTTPException(400, "Future dates are not allowed.")
        params = {"date": dd.isoformat()}
    else:
        raise HTTPException(400, "Provide either date= or start=&end=.")
    data = _guard(provider.fetch_summary, plant, params)
    summaries = [
        {k: srow.get(k) for k in SUMMARY_COLUMNS}
        for srow in (data.get("summaries") or [])
        if srow.get("data_label") != BLOCKED
    ]
    if fmt == "csv":
        return _csv(summaries, SUMMARY_COLUMNS, f"{plant}-summary.csv")
    return {"plant_id": plant, "data_policy": DATA_POLICY, "count": len(summaries), "summaries": summaries}
