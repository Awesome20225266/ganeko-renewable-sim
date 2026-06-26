"""In-process data access for the restricted wrapper.

Reads directly from THIS application's own database + simulation logic — no network
call, no provider API key, no loopback. Returns plain dicts in the same shape the
router expects (timestamps as ISO strings); the router does the forecast filtering.

This is the single-service ("set and forget") design: nothing to activate beyond the
optional wrapper user key, and no provider key that could expire or be revoked.
"""
from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app.api.repository import (
    get_blocks,
    get_blocks_range,
    get_summaries_range,
    get_summary,
)
from app.api.routes_plants import _block_to_out, _series, _summary_to_out
from app.api.schemas import CurrentOut
from app.config.settings import get_settings
from app.db.base import session_scope
from app.logging_conf import get_logger
from app.simulate import ensure_fresh_live, load_active_config

logger = get_logger(__name__)

# Data-label policy shared with the router.
ALLOWED_LABELS = {"LIVE_ESTIMATED", "HISTORICAL_SIMULATED"}
BLOCKED_LABEL = "FORECAST_SIMULATED"


class ProviderNotConfigured(Exception):
    """Retained for router/test compatibility; not raised in in-process mode."""


class ProviderError(Exception):
    """A mapped, user-safe failure (carries an HTTP status + safe message)."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(message)


def _series_dict(plant_id: str, sim_date: date_cls, mode: str, current_block: int | None = None) -> dict:
    """Reuse the main API's series builder, translating its errors to ProviderError."""
    try:
        out = _series(plant_id, sim_date, mode, current_block)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise ProviderError(404, "No data found for the requested parameters") from None
        logger.warning("wrapper _series(%s,%s,%s) failed: %s", plant_id, sim_date, mode, exc.detail)
        raise ProviderError(502, "Renewable data service error") from None
    return out.model_dump(mode="json")


def fetch_current(plant_id: str) -> dict:
    """Latest live reading (mirrors /plants/{id}/current, in-process)."""
    try:
        ensure_fresh_live(plant_id)  # never raises; refreshes today's LIVE if stale
        with session_scope() as db:
            cfg = load_active_config(db, plant_id)
            tz = cfg.timezone
            now = datetime.now(ZoneInfo(tz))
            today = now.date()
            cur_no = now.hour * 4 + now.minute // 15 + 1
            blocks = get_blocks(db, plant_id, today, "LIVE")
            if not blocks:
                raise ProviderError(404, "No live data available yet")
            cur_no = min(cur_no, len(blocks))
            b = blocks[cur_no - 1]
            energy_today = sum(x.total_mwh for x in blocks[:cur_no])
            out = CurrentOut(
                plant_code=plant_id,
                plant_name=cfg.plant_name,
                timezone=tz,
                sim_date=today,
                block_no=b.block_no,
                block_start=b.block_start,
                block_end=b.block_end,
                solar_mw=round(b.solar_mw, 3),
                wind_mw=round(b.wind_mw, 3),
                total_mw=round(b.total_mw, 3),
                solar_ac_mw=cfg.solar_ac_mw,
                wind_ac_mw=cfg.wind_ac_mw,
                hybrid_cuf=round(b.hybrid_cuf, 4),
                energy_today_mwh=round(energy_today, 3),
                data_label="LIVE_ESTIMATED",
                data_quality_status=b.data_quality_status,
                as_of=b.weather_fetch_time,
                refresh_interval_minutes=get_settings().LIVE_REFRESH_MINUTES,
                note="Weather-based simulated estimate of actual generation (not metered).",
            )
            return out.model_dump(mode="json")
    except ProviderError:
        raise
    except ValueError:
        raise ProviderError(404, "Unknown plant") from None
    except Exception as exc:  # noqa: BLE001 — never leak internals to the user
        logger.warning("wrapper fetch_current failed: %s", exc)
        raise ProviderError(502, "Renewable data service error") from None


def fetch_live(plant_id: str) -> dict:
    """Today's full 96-block series (mirrors /plants/{id}/live, in-process)."""
    try:
        ensure_fresh_live(plant_id)
        with session_scope() as db:
            cfg = load_active_config(db, plant_id)
            tz = cfg.timezone
    except ValueError:
        raise ProviderError(404, "Unknown plant") from None
    except Exception as exc:  # noqa: BLE001
        logger.warning("wrapper fetch_live setup failed: %s", exc)
        raise ProviderError(502, "Renewable data service error") from None
    now = datetime.now(ZoneInfo(tz))
    current_block = now.hour * 4 + now.minute // 15 + 1
    return _series_dict(plant_id, now.date(), "LIVE", current_block)


def fetch_historical(plant_id: str, date_iso: str) -> dict:
    """Completed historical day (mirrors /plants/{id}/historical, in-process)."""
    return _series_dict(plant_id, date_cls.fromisoformat(date_iso), "HISTORICAL")


def fetch_range(plant_id: str, start_iso: str, end_iso: str) -> list:
    """Block-wise generation over a date range, grouped per day (in-process)."""
    s = date_cls.fromisoformat(start_iso)
    e = date_cls.fromisoformat(end_iso)
    try:
        with session_scope() as db:
            blocks = get_blocks_range(db, plant_id, s, e)
            by_day: dict[date_cls, list] = {}
            for b in blocks:
                by_day.setdefault(b.sim_date, []).append(b)
            return [
                {
                    "sim_date": d.isoformat(),
                    "blocks": [_block_to_out(b).model_dump(mode="json") for b in by_day[d]],
                }
                for d in sorted(by_day)
            ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("wrapper fetch_range failed: %s", exc)
        raise ProviderError(502, "Renewable data service error") from None


def fetch_summary(plant_id: str, params: dict[str, str]) -> dict:
    """Daily summary for a single date or a range (in-process)."""
    try:
        with session_scope() as db:
            if "start" in params and "end" in params:
                rows = get_summaries_range(
                    db, plant_id,
                    date_cls.fromisoformat(params["start"]),
                    date_cls.fromisoformat(params["end"]),
                )
            else:
                row = get_summary(db, plant_id, date_cls.fromisoformat(params["date"]))
                rows = [row] if row else []
            return {"summaries": [_summary_to_out(s).model_dump(mode="json") for s in rows]}
    except Exception as exc:  # noqa: BLE001
        logger.warning("wrapper fetch_summary failed: %s", exc)
        raise ProviderError(502, "Renewable data service error") from None
