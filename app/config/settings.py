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
    PLANT_NAME: str = "Jaisalmer Hybrid Park"
    PLANT_LAT: float = 26.9124
    PLANT_LON: float = 70.9026
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

    # Dashboard
    DASHBOARD_REFRESH_SECONDS: int = 900
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
