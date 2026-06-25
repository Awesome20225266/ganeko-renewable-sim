"""Dashboard: a self-contained HTML page + open read-only JSON feeds.

The JSON feeds expose only generation/weather data (no secrets, no internal IDs),
so the dashboard needs no API key. The 8 business APIs remain key-protected.
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from app.api.repository import get_blocks, get_summaries_range
from app.config.settings import get_settings
from app.db.base import session_scope
from app.db.models import WeatherBlock
from app.simulate import load_active_config

router = APIRouter(tags=["dashboard"])

_TEMPLATE = Path(__file__).parent / "templates" / "index.html"


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page():
    html = _TEMPLATE.read_text(encoding="utf-8")
    settings = get_settings()
    html = html.replace("__REFRESH_SECONDS__", str(settings.DASHBOARD_REFRESH_SECONDS))
    html = html.replace("__DEFAULT_PLANT__", settings.PLANT_CODE)
    return HTMLResponse(html)


@router.get("/dashboard/api/today/{code}")
def dashboard_today(code: str):
    with session_scope() as db:
        try:
            cfg = load_active_config(db, code)
        except ValueError:
            raise HTTPException(404, f"Unknown plant '{code}'") from None
        tz = cfg.timezone
        now = datetime.now(ZoneInfo(tz))
        today = now.date()
        current_block = now.hour * 4 + now.minute // 15 + 1
        blocks = get_blocks(db, code, today, "LIVE")
        weather = {
            wb.block_no: wb
            for wb in db.query(WeatherBlock)
            .filter(
                WeatherBlock.plant_code == code,
                WeatherBlock.sim_date == today,
                WeatherBlock.data_mode == "LIVE",
            )
            .all()
        }
        out_blocks = []
        for b in blocks:
            wb = weather.get(b.block_no)
            out_blocks.append(
                {
                    "block_no": b.block_no,
                    "time": b.block_start.strftime("%H:%M"),
                    "solar_mw": round(b.solar_mw, 2),
                    "wind_mw": round(b.wind_mw, 2),
                    "total_mw": round(b.total_mw, 2),
                    "data_label": b.data_label,
                    "data_quality_status": b.data_quality_status,
                    "interpolated": (wb.interpolated if wb else False),
                    "ghi": round(wb.ghi, 1) if wb and wb.ghi is not None else None,
                    "poa": round(wb.poa, 1) if wb and wb.poa is not None else None,
                    "temp": round(wb.temperature_2m, 1)
                    if wb and wb.temperature_2m is not None
                    else None,
                    "wind_speed": round(wb.wind_speed_100m, 2)
                    if wb and wb.wind_speed_100m is not None
                    else None,
                }
            )
        cum_solar = sum(b["solar_mw"] for b in out_blocks[:current_block]) * 0.25
        cum_wind = sum(b["wind_mw"] for b in out_blocks[:current_block]) * 0.25
        cur = out_blocks[current_block - 1] if 0 < current_block <= len(out_blocks) else None
        return {
            "plant_code": code,
            "plant_name": cfg.plant_name,
            "date": today.isoformat(),
            "timezone": tz,
            "solar_ac_mw": cfg.solar_ac_mw,
            "wind_ac_mw": cfg.wind_ac_mw,
            "current_block_no": current_block,
            "current": cur,
            "cumulative_solar_mwh": round(cum_solar, 2),
            "cumulative_wind_mwh": round(cum_wind, 2),
            "cumulative_total_mwh": round(cum_solar + cum_wind, 2),
            "blocks": out_blocks,
        }


@router.get("/dashboard/api/history/{code}")
def dashboard_history(code: str, days: int = 7):
    days = max(1, min(days, 31))
    with session_scope() as db:
        try:
            cfg = load_active_config(db, code)
        except ValueError:
            raise HTTPException(404, f"Unknown plant '{code}'") from None
        today = datetime.now(ZoneInfo(cfg.timezone)).date()
        start = today - timedelta(days=days)
        rows = get_summaries_range(db, code, start, today - timedelta(days=1))
        return {
            "plant_code": code,
            "summaries": [
                {
                    "date": s.sim_date.isoformat(),
                    "solar_mwh": round(s.solar_mwh, 1),
                    "wind_mwh": round(s.wind_mwh, 1),
                    "total_mwh": round(s.total_mwh, 1),
                    "hybrid_cuf": round(s.hybrid_cuf * 100, 1),
                    "data_label": s.data_label,
                }
                for s in rows
            ],
        }
