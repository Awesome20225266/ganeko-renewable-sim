"""Server-side HTTP client for the external Renewable Generation API (provider).

The provider key lives ONLY here (read from settings) and is never returned to the
caller, logged, or echoed in errors. The backend controls exactly which provider
path is called — callers can never supply an arbitrary URL/path (so /forecast is
simply unreachable through the wrapper).
"""
from __future__ import annotations

from typing import Any

import httpx

from app.config.settings import get_settings
from app.logging_conf import get_logger

logger = get_logger(__name__)

# Data-label policy shared with the router.
ALLOWED_LABELS = {"LIVE_ESTIMATED", "HISTORICAL_SIMULATED"}
BLOCKED_LABEL = "FORECAST_SIMULATED"


class ProviderNotConfigured(Exception):
    """The server is missing RENEWABLE_API_KEY — a configuration error, not the user's."""


class ProviderError(Exception):
    """A mapped, user-safe provider failure (carries an HTTP status + safe message)."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(message)


def _provider_get(path: str, params: dict[str, Any] | None = None) -> Any:
    """GET a FIXED provider path and return parsed JSON, or raise a mapped error.

    `path` is built by the wrapper from trusted, fixed templates — never from raw
    user input — so there is no path-injection / SSRF surface.
    """
    settings = get_settings()
    if not settings.RENEWABLE_API_KEY:
        raise ProviderNotConfigured("RENEWABLE_API_KEY is not set")

    base = settings.RENEWABLE_API_BASE_URL.rstrip("/")
    headers = {
        "X-API-Key": settings.RENEWABLE_API_KEY,  # server-side only, never surfaced
        "User-Agent": "renewable-wrapper/1.0",
    }
    try:
        with httpx.Client(timeout=settings.RENEWABLE_API_TIMEOUT_SECONDS) as client:
            resp = client.get(f"{base}{path}", params=params, headers=headers)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        logger.warning("provider transport error on %s: %s", path, exc)
        raise ProviderError(502, "Renewable data provider unavailable") from None

    if resp.status_code == 200:
        try:
            return resp.json()
        except ValueError:
            logger.warning("provider returned non-JSON on %s", path)
            raise ProviderError(502, "Renewable data provider unavailable") from None
    if resp.status_code == 429:
        raise ProviderError(429, "Rate limit exceeded")
    if resp.status_code in (401, 403):
        # The wrapper's OWN provider credentials were rejected — a server-side
        # misconfiguration. Never tell the user it was an auth problem (would leak).
        logger.error("provider rejected wrapper credentials on %s (status %s)", path, resp.status_code)
        raise ProviderError(502, "Renewable data provider unavailable")
    if resp.status_code == 404:
        raise ProviderError(404, "No data found for the requested parameters")
    if resp.status_code == 400:
        raise ProviderError(400, "Invalid request")
    logger.warning("provider unexpected status %s on %s", resp.status_code, path)
    raise ProviderError(502, "Renewable data provider unavailable")


# --- Fixed-path accessors (the only ways out to the provider) ----------------
def fetch_current(plant_id: str) -> dict:
    return _provider_get(f"/plants/{plant_id}/current")


def fetch_live(plant_id: str) -> dict:
    return _provider_get(f"/plants/{plant_id}/live")


def fetch_historical(plant_id: str, date_iso: str) -> dict:
    return _provider_get(f"/plants/{plant_id}/historical", {"date": date_iso})


def fetch_range(plant_id: str, start_iso: str, end_iso: str) -> list:
    return _provider_get(f"/plants/{plant_id}/range", {"start": start_iso, "end": end_iso})


def fetch_summary(plant_id: str, params: dict[str, str]) -> dict:
    return _provider_get(f"/plants/{plant_id}/summary", params)
