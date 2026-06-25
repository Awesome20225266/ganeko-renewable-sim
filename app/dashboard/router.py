"""Dashboard: a self-contained HTML page + open read-only JSON feeds.

The JSON feeds expose only generation/weather data (no secrets, no internal IDs),
so the dashboard needs no API key. The 8 business APIs remain key-protected.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from app.api.repository import (
    get_blocks,
    get_summaries_range,
    get_weather_blocks,
)
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


def _gen_block_dict(b) -> dict:
    return {
        "block_no": b.block_no,
        "time": b.block_start.strftime("%H:%M"),
        "solar_mw": round(b.solar_mw, 2),
        "wind_mw": round(b.wind_mw, 2),
        "total_mw": round(b.total_mw, 2),
        "data_label": b.data_label,
        "data_quality_status": b.data_quality_status,
    }


@router.get("/dashboard/api/config/{code}")
def dashboard_config(code: str):
    """Current active config for prefilling the Config tab (read-only, no secrets)."""
    with session_scope() as db:
        try:
            cfg = load_active_config(db, code)
        except ValueError:
            raise HTTPException(404, f"Unknown plant '{code}'") from None
        return {
            "plant_code": cfg.plant_code,
            "plant_name": cfg.plant_name,
            "latitude": cfg.latitude,
            "longitude": cfg.longitude,
            "timezone": cfg.timezone,
            "config_version": cfg.config_version,
            "solar_ac_mw": cfg.solar_ac_mw,
            "solar_dc_mw": cfg.solar_dc_mw,
            "dc_ac_ratio": cfg.dc_ac_ratio,
            "solar_performance_ratio": cfg.solar_performance_ratio,
            "solar_loss_factor": cfg.solar_loss_factor,
            "temp_coeff_pct_per_c": cfg.temp_coeff_pct_per_c,
            "panel_tilt": cfg.panel_tilt,
            "panel_azimuth": cfg.panel_azimuth,
            "use_global_tilted_irradiance": cfg.use_global_tilted_irradiance,
            "wind_ac_mw": cfg.wind_ac_mw,
            "wind_loss_factor": cfg.wind_loss_factor,
            "hub_height_m": cfg.hub_height_m,
            "cut_in_ms": cfg.cut_in_ms,
            "rated_ms": cfg.rated_ms,
            "cut_out_ms": cfg.cut_out_ms,
            "air_density_correction": cfg.air_density_correction,
        }


@router.get("/dashboard/api/day/{code}")
def dashboard_day(code: str, date: str, mode: str = "HISTORICAL"):
    """Generation blocks for any date + mode (read-only feed for the Explore tab)."""
    from datetime import date as _date

    d = _date.fromisoformat(date)
    with session_scope() as db:
        blocks = get_blocks(db, code, d, mode.upper())
        summaries = get_summaries_range(db, code, d, d)
        s = next((x for x in summaries if x.data_mode == mode.upper()), None)
        return {
            "plant_code": code,
            "date": d.isoformat(),
            "mode": mode.upper(),
            "data_label": blocks[0].data_label if blocks else None,
            "block_count": len(blocks),
            "summary": (
                {
                    "solar_mwh": round(s.solar_mwh, 1),
                    "wind_mwh": round(s.wind_mwh, 1),
                    "total_mwh": round(s.total_mwh, 1),
                    "solar_cuf": round(s.solar_cuf * 100, 1),
                    "wind_cuf": round(s.wind_cuf * 100, 1),
                    "hybrid_cuf": round(s.hybrid_cuf * 100, 1),
                    "solar_specific_yield": round(s.solar_specific_yield, 2),
                }
                if s
                else None
            ),
            "blocks": [_gen_block_dict(b) for b in blocks],
        }


@router.get("/dashboard/api/weather/{code}")
def dashboard_weather(code: str, date: str, mode: str = "LIVE"):
    """Normalized weather variables for any date + mode (Weather tab)."""
    from datetime import date as _date

    d = _date.fromisoformat(date)
    with session_scope() as db:
        blocks = get_weather_blocks(db, code, d, mode.upper())
        return {
            "plant_code": code,
            "date": d.isoformat(),
            "mode": mode.upper(),
            "block_count": len(blocks),
            "weather_source": blocks[0].weather_source if blocks else None,
            "blocks": [
                {
                    "block_no": w.block_no,
                    "time": w.block_start.strftime("%H:%M"),
                    "ghi": round(w.ghi, 1) if w.ghi is not None else None,
                    "poa": round(w.poa, 1) if w.poa is not None else None,
                    "dni": round(w.dni, 1) if w.dni is not None else None,
                    "dhi": round(w.dhi, 1) if w.dhi is not None else None,
                    "temp": round(w.temperature_2m, 1) if w.temperature_2m is not None else None,
                    "cloud_cover": round(w.cloud_cover, 1) if w.cloud_cover is not None else None,
                    "is_day": w.is_day,
                    "wind_speed_100m": round(w.wind_speed_100m, 2)
                    if w.wind_speed_100m is not None
                    else None,
                    "wind_gusts_10m": round(w.wind_gusts_10m, 2)
                    if w.wind_gusts_10m is not None
                    else None,
                    "wind_direction_100m": w.wind_direction_100m,
                    "surface_pressure": round(w.surface_pressure, 1)
                    if w.surface_pressure is not None
                    else None,
                    "interpolated": w.interpolated,
                }
                for w in blocks
            ],
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
