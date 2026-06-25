"""Idempotent seed: initial plant (160/240 MW solar + 135 MW wind), weather provider,
simulation-version registry entry, and one admin API key.

Re-running is safe: every insert is guarded by an existence check. The admin key is
reused if ADMIN_BOOTSTRAP_KEY is set; otherwise a random key is generated and printed.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from app.config.settings import get_settings
from app.db.base import init_db, session_scope
from app.db.models import (
    ApiKey,
    Plant,
    PlantConfig,
    SimulationVersion,
    WeatherProviderConfig,
)
from app.security import generate_api_key, hash_key, key_prefix

# A realistic farm-level wind power curve (m/s -> kW) for a 135 MW farm.
# Rated reached at 12 m/s; flat to cut-out 25 m/s; zero below 3 m/s cut-in.
WIND_RATED_KW = 135_000.0
_CURVE_FRACTIONS = {
    0: 0.0, 1: 0.0, 2: 0.0, 3: 0.0,
    4: 0.030, 5: 0.080, 6: 0.160, 7: 0.260, 8: 0.400,
    9: 0.550, 10: 0.720, 11: 0.880, 12: 1.0,
    13: 1.0, 14: 1.0, 15: 1.0, 16: 1.0, 17: 1.0, 18: 1.0,
    19: 1.0, 20: 1.0, 21: 1.0, 22: 1.0, 23: 1.0, 24: 1.0, 25: 1.0,
    26: 0.0,
}
WIND_POWER_CURVE = [[float(s), round(f * WIND_RATED_KW, 3)] for s, f in sorted(_CURVE_FRACTIONS.items())]

SOLAR_VARIABLES = [
    "shortwave_radiation",
    "direct_radiation",
    "diffuse_radiation",
    "direct_normal_irradiance",
    "temperature_2m",
    "cloud_cover",
    "is_day",
]
WIND_VARIABLES = [
    "wind_speed_10m",
    "wind_speed_100m",
    "wind_speed_120m",
    "wind_speed_180m",
    "wind_direction_100m",
    "wind_gusts_10m",
    "temperature_2m",
    "surface_pressure",
]


def run_seed() -> dict:
    """Seed baseline data. Returns a summary dict (incl. admin key iff newly created)."""
    settings = get_settings()
    init_db()
    result: dict = {"created": [], "existing": []}

    with session_scope() as db:
        # --- Plant + config -------------------------------------------------
        plant = db.scalar(select(Plant).where(Plant.plant_code == settings.PLANT_CODE))
        if plant is None:
            plant = Plant(
                plant_code=settings.PLANT_CODE,
                plant_name=settings.PLANT_NAME,
                active_config_version=1,
            )
            db.add(plant)
            db.flush()
            cfg = PlantConfig(
                plant_id=plant.id,
                plant_code=settings.PLANT_CODE,
                config_version=1,
                is_active=True,
                plant_name=settings.PLANT_NAME,
                latitude=settings.PLANT_LAT,
                longitude=settings.PLANT_LON,
                timezone=settings.PLANT_TZ,
                # Solar — confirmed from reference workbook
                solar_ac_mw=160.0,
                solar_dc_mw=240.0,
                dc_ac_ratio=1.5,
                solar_performance_ratio=0.80,
                solar_loss_factor=0.10,
                temp_coeff_pct_per_c=-0.40,
                panel_tilt=25.0,
                panel_azimuth=180.0,
                use_global_tilted_irradiance=True,
                # Wind
                wind_ac_mw=135.0,
                wind_loss_factor=0.08,
                wind_power_curve=WIND_POWER_CURVE,
                curve_rated_kw=WIND_RATED_KW,
                hub_height_m=100.0,
                cut_in_ms=3.0,
                rated_ms=12.0,
                cut_out_ms=25.0,
                air_density_correction=True,
                block_minutes=15,
                api_access_rules={"read_scopes": ["read", "admin"]},
            )
            db.add(cfg)
            result["created"].append("plant + plant_config v1")
        else:
            result["existing"].append("plant")

        # --- Weather provider ----------------------------------------------
        provider = db.scalar(
            select(WeatherProviderConfig).where(WeatherProviderConfig.name == "open-meteo")
        )
        if provider is None:
            db.add(
                WeatherProviderConfig(
                    name="open-meteo",
                    forecast_url=settings.OPEN_METEO_FORECAST_URL,
                    historical_forecast_url=settings.OPEN_METEO_HISTORICAL_FORECAST_URL,
                    archive_url=settings.OPEN_METEO_ARCHIVE_URL,
                    solar_variables=SOLAR_VARIABLES,
                    wind_variables=WIND_VARIABLES,
                    enabled=True,
                )
            )
            result["created"].append("weather_provider_config(open-meteo)")
        else:
            result["existing"].append("weather_provider_config")

        # --- Simulation version registry -----------------------------------
        ver = db.scalar(
            select(SimulationVersion).where(
                SimulationVersion.version == settings.SIMULATION_VERSION
            )
        )
        if ver is None:
            db.add(
                SimulationVersion(
                    version=settings.SIMULATION_VERSION,
                    description="Initial physics-based solar/wind/hybrid simulation engine.",
                    model_assumption_version=settings.MODEL_ASSUMPTION_VERSION,
                    assumptions={
                        "solar": "POA->cell-temp derate->DC->inverter clip",
                        "wind": "hub-height power-law + power-curve interpolation + optional air-density",
                        "noct_c": 45,
                    },
                )
            )
            result["created"].append(f"simulation_version({settings.SIMULATION_VERSION})")
        else:
            result["existing"].append("simulation_version")

        # --- Admin API key --------------------------------------------------
        admin_exists = db.scalar(select(ApiKey).where(ApiKey.scope == "admin"))
        if admin_exists is None:
            raw = settings.ADMIN_BOOTSTRAP_KEY or generate_api_key("admin")
            existing_hash = db.scalar(
                select(ApiKey).where(ApiKey.key_hash == hash_key(raw))
            )
            if existing_hash is None:
                db.add(
                    ApiKey(
                        team=settings.ADMIN_KEY_TEAM,
                        name="bootstrap-admin",
                        key_prefix=key_prefix(raw),
                        key_hash=hash_key(raw),
                        scope="admin",
                        is_active=True,
                        rate_limit_per_min=600,
                        created_at=datetime.now(UTC),
                    )
                )
                result["created"].append("admin api_key")
                result["admin_key"] = raw
        else:
            result["existing"].append("admin api_key")

    return result


if __name__ == "__main__":
    summary = run_seed()
    print("Seed complete.")
    print("  created :", summary.get("created") or "nothing (already seeded)")
    print("  existing:", summary.get("existing"))
    if "admin_key" in summary:
        print("\n  >>> ADMIN API KEY (store securely, shown once):")
        print("      " + summary["admin_key"])
    else:
        print("\n  Admin key already present (not re-printed).")
