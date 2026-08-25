"""Application settings, loaded from environment / .env. Nothing is hardcoded."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Database
    DATABASE_URL: str = "sqlite:///./data/renewable.db"

    # Admin bootstrap
    ADMIN_BOOTSTRAP_KEY: str = "admin-dev-key-change-me"
    ADMIN_KEY_TEAM: str = "platform-admin"

    # Seed plant (configurable placeholder location)
    PLANT_CODE: str = "HYBRID01"
    PLANT_NAME: str = "Ganeko Two Energy Private Limited"
    PLANT_LAT: float = 17.60
    PLANT_LON: float = 76.36
    PLANT_TZ: str = "Asia/Kolkata"

    # Open-Meteo
    OPEN_METEO_FORECAST_URL: str = "https://api.open-meteo.com/v1/forecast"
    OPEN_METEO_HISTORICAL_FORECAST_URL: str = (
        "https://historical-forecast-api.open-meteo.com/v1/forecast"
    )
    OPEN_METEO_ARCHIVE_URL: str = "https://archive-api.open-meteo.com/v1/archive"
    OPEN_METEO_TIMEOUT_SECONDS: float = 30.0
    OPEN_METEO_MAX_RETRIES: int = 4

    # Scheduler
    SCHEDULER_ENABLED: bool = True
    SCHEDULER_DAILY_TIME: str = "00:30"
    LIVE_REFRESH_MINUTES: int = 15

    # Forecast-horizon prefetch. The daily job is pinned to SCHEDULER_DAILY_TIME in
    # plant-local time (00:30 IST = 19:00 UTC) because that is a publication deadline.
    # But Open-Meteo's free quota — shared across the host's egress IP — is reliably
    # spent from ~07:00 UTC until it resets at 00:00 UTC, so a forecast fetch at
    # 19:00 UTC returns 429 every night. Fetching the horizon in its own job just after
    # the reset is the only slot where it can actually be refreshed.
    # Pinned to UTC on purpose: the quota resets in UTC, not in plant-local time.
    FORECAST_PREFETCH_ENABLED: bool = True
    # Runs hourly across this inclusive UTC hour range and is a cheap no-op once the
    # horizon is fresh, so a morning that is still rate-limited just retries next hour.
    FORECAST_PREFETCH_START_HOUR_UTC: int = 1
    FORECAST_PREFETCH_END_HOUR_UTC: int = 6
    # A stored forecast younger than this counts as fresh and is not refetched. Must stay
    # under 24h or a date would never be refreshed on subsequent days.
    FORECAST_PREFETCH_MAX_AGE_HOURS: int = 20

    # Keep-alive: self-ping the public URL so a free-tier host (Render/Railway/Fly)
    # never spins down on idle. Render auto-injects RENDER_EXTERNAL_URL; if KEEPALIVE_URL
    # is blank we fall back to that. Ping interval must be < the host's idle threshold
    # (Render = 15 min), so 10 min keeps the service permanently awake.
    KEEPALIVE_ENABLED: bool = False
    KEEPALIVE_URL: str = ""
    KEEPALIVE_MINUTES: int = 10

    # Dashboard
    DASHBOARD_REFRESH_SECONDS: int = 60
    # The dashboard console performs config edits / key generation / simulation runs
    # WITHOUT an API key (it is the trusted same-origin admin console). Keep this true
    # for local/trusted use; set false in production to disable those write actions and
    # require the key-protected /plants & /admin APIs instead. Read-only feeds stay on.
    DASHBOARD_CONSOLE_WRITE: bool = True

    # API / security
    DEFAULT_RATE_LIMIT_PER_MIN: int = 120
    API_KEY_HEADER: str = "X-API-Key"
    # Comma-separated list of allowed CORS origins ("*" = any). Needed when other
    # users fetch the API from a different origin / front-end.
    CORS_ALLOW_ORIGINS: str = "*"

    # --- Restricted user-facing wrapper API (/api/renewable/*) ------------------
    # Exposes ONLY live + historical data (never forecast). It reads this app's own
    # data in-process — no provider key or outbound call needed. The only thing to
    # set is the optional shared user key below.
    RENEWABLE_PLANT_ID: str = "HYBRID01"
    RENEWABLE_PLANT_TZ: str = "Asia/Kolkata"  # used to compute "today" for date checks
    # Optional shared key external users/Excel must send as X-API-Key. Blank = open.
    # Set this to one value you hand to your users before sharing the wrapper.
    RENEWABLE_WRAPPER_USER_API_KEY: str = ""

    # --- Day-ahead P90 schedule -------------------------------------------------
    # Anchored on the simulated generation, carrying a realistic (non-uniform)
    # day-ahead forecast error. Issued day-ahead, then revised FORWARD-ONLY: the
    # block we are inside and every block before it keep their published values, so
    # the commitment a deviation is measured against never moves.
    SCHEDULE_ENABLED: bool = True
    SCHEDULE_VERSION: str = "p90-v1.0.0"
    # How far ahead the daily job issues. MUST stay at 1 for genuine day-ahead
    # semantics: a horizon of N means a date's schedule is first published N days
    # early, off an N-day-out forecast. At N=1 each date is issued at 00:30 the day
    # before, off the freshest forecast — which is what the accuracy model is
    # calibrated for. (Intraday revision improves the future part of a published
    # schedule, but it cannot retro-improve the blocks that have already passed.)
    SCHEDULE_HORIZON_DAYS: int = 1
    # Refuse to publish a "day-ahead" schedule anchored on weather older than this.
    # Without the guard, a rate-limited forecast step silently re-simulates from whatever
    # weather is already stored, so schedules keep publishing off a days-old forecast and
    # every health signal looks green. That is exactly how a 7-night provider outage went
    # unnoticed until the stored horizon ran out. 36h covers a normal prefetch-then-
    # publish cycle (~18h) with headroom; beyond that we fail loudly instead.
    SCHEDULE_MAX_ANCHOR_AGE_HOURS: int = 36
    # Hourly maintenance pass: re-attempt any schedule the daily job could not issue,
    # and revise the FUTURE part of schedules that already exist.
    SCHEDULE_RETRY_ENABLED: bool = True
    # Automatic intraday revision. When a schedule already exists, the hourly pass
    # re-issues it so blocks beyond the lock window below pick up newer forecast
    # information; everything inside the window is protected in the single writer, so
    # committed blocks cannot move. Costs zero provider calls — a revision reads
    # stored generation/weather and recomputes P90 in process. Set false to freeze
    # whole days on first issue.
    SCHEDULE_INTRADAY_REVISION_ENABLED: bool = True
    # Operational lock-in window, in minutes AFTER the current block. While we are
    # operating inside block X, the schedule for this much time ahead counts as
    # committed dispatch and a new forecast must not rewrite it. 120 minutes on a
    # 15-minute grid protects X plus the next 8 blocks, so the first revisable block
    # is X+9 (operating in 15:45-16:00 -> everything through 17:45-18:00 holds,
    # 18:00-18:15 may move).
    #
    # Configurable because it is an operational/market parameter, not a physical
    # constant. 0 disables the lock and restores "current block only". Values that
    # are not a multiple of the 15-minute grid are rounded UP, so the protected
    # window is never shorter than configured. The window never crosses midnight:
    # each date carries its own independent schedule.
    SCHEDULE_REVISION_LOCK_MINUTES: int = 120
    # Master accuracy knob. 1.15 -> ~10% MAPE / ~2% nMAE of capacity ("P90" =
    # ~90% accurate). Raise for a looser schedule, lower for a tighter one.
    SCHEDULE_SIGMA_SCALE: float = 1.15
    # Bounds the worst sustained relative miss. Without it a tail excursion in the
    # error process drives the schedule far below actual for hours on end.
    SCHEDULE_REL_SIGMA_CAP: float = 0.32
    SCHEDULE_DAY_ENERGY_CAP: float = 0.05   # max |day MWh deviation| vs actual

    # --- Publication invariants: immutable Actuals, forward-only Schedule -------
    # An ACTUAL (today's already-reached blocks, and a completed day) is a statement
    # of record: once published it must never be recalculated, even though Open-Meteo
    # keeps revising its own view of the past (measured: +19% to +56% on already-
    # elapsed intervals). A SCHEDULE is a forward commitment: new forecast
    # information may revise it, but only for blocks whose operational time has not
    # arrived — never the block we are inside or one already past.
    #
    # This is the instant enforcement SWITCHES ON, as an ISO-8601 timestamp WITH a
    # UTC offset. It is not a per-block filter: before it the old behaviour stands,
    # from it enforcement is unconditional and permanent. Nothing already stored is
    # rewritten by the transition, which is what makes the change forward-only and
    # removes any need for a migration or backfill. HISTORICAL actuals and schedules
    # are final regardless of this value. Once it has passed the setting stops
    # mattering and may be left at its default forever.
    #
    #   local / UAT : leave blank to enforce everywhere (no legacy rows to protect)
    #   production  : the agreed cutover instant, below
    #
    # Blank, naive (no offset) or malformed => enforce for ALL blocks. The fail-safe
    # direction protects data rather than exposing it; a typo must not silently
    # switch a data-integrity invariant off.
    GENERATION_IMMUTABILITY_CUTOVER: str = "2026-08-25T15:00:00+05:30"

    # App
    LOG_LEVEL: str = "INFO"
    SIMULATION_VERSION: str = "v1.0.0"
    MODEL_ASSUMPTION_VERSION: str = "v1.0.0"
    # Adds deterministic, weather-correlated block-to-block variability (cloud-driven
    # solar flicker, wind turbulence) so output resembles real metered plant data.
    # Reproducible (seeded by block timestamp) and bounded (caps/night/no-negatives hold).
    REALISM_TEXTURE: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
