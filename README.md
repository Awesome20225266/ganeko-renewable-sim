# Renewable Generation Simulation Platform

Solar / Wind / Hybrid generation simulation from **live Open-Meteo weather data**.
For any plant and any date — past (historical), today (live), or future (forecast) — it
produces **96 fifteen-minute blocks/day** of solar, wind and hybrid generation, exposes
them through 8 API-key-protected REST endpoints, runs a daily scheduler, and serves a
live dashboard.

- **Stack:** Python 3.11+, FastAPI, SQLAlchemy + Alembic, APScheduler, pandas/numpy, httpx.
- **DB:** SQLite for zero-config local use; PostgreSQL via docker-compose.
- **Weather:** live Open-Meteo (no API key required for non-commercial use).

---

## 1. Quick start (under 10 minutes)

### Option A — Docker (api + scheduler + postgres + dashboard)
```bash
git clone <repo> && cd "API Creation"
cp .env.example .env          # adjust if you like; defaults work out of the box
docker compose up --build
```
- API + docs: <http://localhost:8000/docs>
- Dashboard:  <http://localhost:8000/dashboard> (also exposed on :8001)
- Migrations + idempotent seed run automatically on start.
- The seeded admin key is `ADMIN_BOOTSTRAP_KEY` from your `.env` (default `admin-dev-key-change-me`).

### Option B — Local, no Docker (SQLite)
```bash
cp .env.example .env
python -m pip install -r requirements.txt
python -m alembic upgrade head        # create schema  (Windows: use `py`)
python -m app.db.seed                 # seed plant + admin key (prints the key once)
python -m uvicorn app.main:app --port 8000
```
> On Windows the Python launcher is `py`; substitute it for `python` (e.g. `py -m alembic upgrade head`).
> `make` targets accept `PYTHON=py`, e.g. `make seed PYTHON=py`.

Populate some data, then open the dashboard:
```bash
python -m app.cli simulate --date 2026-06-20 --mode HISTORICAL
python -m app.cli simulate --date 2026-06-25 --mode LIVE
python -m app.cli simulate --date 2026-06-27 --mode FORECAST
```

---

## 2. The 8 APIs (all require a key)

All `/plants` and `/admin` endpoints require the header `X-API-Key: <key>` (header name
configurable via `API_KEY_HEADER`). Every response carries a **data label**:
`HISTORICAL_SIMULATED | LIVE_ESTIMATED | FORECAST_SIMULATED | REPROCESSED | PARTIAL | FAILED | FINALIZED`.

```bash
KEY=admin-dev-key-change-me
B=http://localhost:8000

# 1) Plant config (capacities, location, timezone, active assumptions)
curl -H "X-API-Key: $KEY" $B/plants/HYBRID01/config

# 2) Historical (completed date) — block-wise
curl -H "X-API-Key: $KEY" "$B/plants/HYBRID01/historical?date=2026-06-20"

# 3) Live (today; completed blocks LIVE_ESTIMATED, remaining FORECAST_SIMULATED)
curl -H "X-API-Key: $KEY" $B/plants/HYBRID01/live

# 4) Forecast (by explicit date or horizon days: 0=rest of today,1,3,7,...)
curl -H "X-API-Key: $KEY" "$B/plants/HYBRID01/forecast?date=2026-06-27"
curl -H "X-API-Key: $KEY" "$B/plants/HYBRID01/forecast?horizon_days=3"

# 5) Daily summary (single date or range)
curl -H "X-API-Key: $KEY" "$B/plants/HYBRID01/summary?date=2026-06-20"
curl -H "X-API-Key: $KEY" "$B/plants/HYBRID01/summary?start=2026-06-18&end=2026-06-20"

# 6) Block-wise over a date range (grouped per day, max 31 days)
curl -H "X-API-Key: $KEY" "$B/plants/HYBRID01/range?start=2026-06-20&end=2026-06-25"

# 7) Reprocess (ADMIN) — new versioned outputs, prior versions preserved
curl -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"plant_code":"HYBRID01","dates":["2026-06-20"],"mode":"HISTORICAL"}' \
     $B/admin/reprocess

# 8) API-key management (ADMIN): create / list / revoke / rotate
curl -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"team":"trading","name":"read-bot","scope":"read","rate_limit_per_min":120}' \
     $B/admin/api-keys
curl -H "X-API-Key: $KEY" $B/admin/api-keys
curl -X DELETE -H "X-API-Key: $KEY" $B/admin/api-keys/<key_prefix>
curl -X POST   -H "X-API-Key: $KEY" $B/admin/api-keys/<key_prefix>/rotate
```
Unauthorized requests get **401** (missing/invalid/expired key) or **403** (read key on an
admin endpoint). Each key has a per-minute rate limit (429 when exceeded); all calls are
logged to `api_usage_log`. Keys are stored **hashed (SHA-256)** — never in plaintext.

---

## 3. Simulation modes & the engines

| Mode | Endpoint chosen | Label |
|---|---|---|
| Future (today's remaining + future days) | `/v1/forecast` (+`minutely_15`) | `FORECAST_SIMULATED` |
| Today / near-real-time | `/v1/forecast` `past_days=1&forecast_days=2` | `LIVE_ESTIMATED` |
| Recent past (≤ ~2 yrs) | `historical-forecast-api` | `HISTORICAL_SIMULATED` |
| Deep historical | `archive-api` (ERA5, **hourly** → interpolated to 15-min) | `HISTORICAL_SIMULATED` |

**Solar:** `POA → cell-temp derate (NOCT 45°C) → DC = DCcap·(POA/1000)·PR·tempFactor·(1−loss) → AC = min(DC, ACcap)`, night-zeroed.
**Wind:** hub-height speed (nearest Open-Meteo level, else power-law `α=0.143`) → optional air-density correction → power-curve interpolation (cut-in/rated/cut-out enforced) → `(1−loss)`, capped at AC.
**Hybrid:** `total = solar + wind`, per-block CUFs, status flags.

> Resolution note: native `minutely_15` exists only for Central Europe & North America; the
> ERA5 archive is hourly. The normalization layer **always** produces a clean 96-block grid,
> interpolating hourly data when needed and **flagging interpolated blocks**.

---

## 4. Adding a new plant (no code changes)

Insert a `plant` + `plant_config` row — the engines, APIs, scheduler and dashboard all pick
it up automatically. Example (SQLite/psql or a small script):
```python
from app.db.base import session_scope
from app.db.models import Plant, PlantConfig
from app.db.seed import WIND_POWER_CURVE, WIND_RATED_KW

with session_scope() as db:
    p = Plant(plant_code="SOLAR02", plant_name="Desert Solar", active_config_version=1)
    db.add(p); db.flush()
    db.add(PlantConfig(
        plant_id=p.id, plant_code="SOLAR02", config_version=1, is_active=True,
        plant_name="Desert Solar", latitude=27.0, longitude=71.0, timezone="Asia/Kolkata",
        solar_ac_mw=100, solar_dc_mw=150, dc_ac_ratio=1.5, solar_performance_ratio=0.8,
        solar_loss_factor=0.1, temp_coeff_pct_per_c=-0.4, panel_tilt=25, panel_azimuth=180,
        use_global_tilted_irradiance=True, wind_ac_mw=0, wind_loss_factor=0.08,
        wind_power_curve=WIND_POWER_CURVE, curve_rated_kw=WIND_RATED_KW, hub_height_m=100,
        cut_in_ms=3, rated_ms=12, cut_out_ms=25, air_density_correction=True,
        block_minutes=15, api_access_rules={}))
```
Then `python -m app.cli simulate --plant SOLAR02 --date <date>`.

Config is **versioned**: change assumptions by inserting a new `config_version` (mark it
`is_active`); old outputs are preserved and new runs reference the new version.

---

## 5. Scheduler & manual runs

- **Daily job** (`SCHEDULER_DAILY_TIME`, plant-local): finalizes yesterday (HISTORICAL),
  refreshes today (LIVE), and builds the +1..+7-day forecast for every active plant.
- **Live refresh** every `LIVE_REFRESH_MINUTES` re-runs today's LIVE simulation.
- Run jobs manually:
  ```bash
  python -m app.cli run-daily
  python -m app.cli live-refresh
  python -m app.cli simulate --date 2024-06-21 --mode HISTORICAL   # any past date
  python -m app.cli reprocess --dates 2026-06-20 2026-06-21
  ```
- **Cron / Celery-beat alternative:** disable the in-process scheduler
  (`SCHEDULER_ENABLED=false`) and schedule `python -m app.cli run-daily` from cron, or wire
  `run_daily_job` into a Celery-beat schedule.

---

## 6. Dashboard

`/dashboard` shows today's solar/wind/hybrid curves, current-block & cumulative generation,
forecast for the remaining blocks, previous-days history, the weather variables used, and a
data-quality badge. **Completed / live-estimated / forecast / interpolated** blocks are
visually distinguished (solid vs dashed line; triangle markers for interpolated). It
auto-refreshes every `DASHBOARD_REFRESH_SECONDS` (default 900). The dashboard reads
open, read-only JSON feeds (no secrets, no internal IDs); the 8 business APIs stay key-protected.

---

## 7. Data storage & versioning

Separate tables: versioned `plant_config`, `weather_provider_config`, **`raw_weather_response`**
(verbatim JSON + URL + fetch time), normalized `weather_block`, `generation_block` (the
per-block hybrid record — solar+wind+total, independently queryable by `data_mode`),
`daily_summary`, `api_key`, `api_usage_log`, `simulation_run`, `error_log`, `simulation_version`.

History is **never overwritten**: reprocessing demotes prior `is_current` rows and writes
new versioned rows (status `REPROCESSED`). Every output row records `simulation_version`,
`model_assumption_version`, `plant_config_version`, `weather_source`, `weather_fetch_time`,
`processed_at`.

> Design note: the spec lists "solar/wind/hybrid output" as separate stores; since the hybrid
> engine emits one per-block record holding all three, they live in a single
> `generation_block` table (queryable per `data_mode`) to avoid 3× write amplification.

---

## 8. Tests & quality

```bash
python -m pytest -q          # engines, normalization, data-quality, API auth
python -m ruff check app tests
```
Data-quality checks (enforced and tested): exactly 96 blocks, no duplicate/missing
timestamps, no negatives, solar = 0 at night, `solar_mw ≤ AC`, `wind_mw ≤ AC`,
`total = solar + wind`. Failing checks mark the run `PARTIAL`/`FAILED` and are logged.

See **HEALTH_REPORT.md** for the full system-health + simulation-realism validation.

---

## 9. Auto-resume during long builds

`scripts/auto_resume.sh` is the actual "auto-continue" mechanism: because the model cannot
self-trigger after a usage limit, this wrapper re-invokes Claude Code in headless continue
mode, backs off on rate-limit signals, and loops until the `BUILD_COMPLETE` marker exists.
It uses `--dangerously-skip-permissions` to run unattended — **only run it in a directory you
trust.** Build state lives in `BUILD_STATE.json` / `PROGRESS.md`.

---

## 10. Configuration (`.env`)

Every setting is in `.env.example` with comments: `DATABASE_URL`, plant location
(`PLANT_LAT/PLANT_LON/PLANT_TZ` — a configurable placeholder; the bundled reference workbook
has no coordinates, so the seed defaults to Jaisalmer, Rajasthan), Open-Meteo base URLs,
scheduler cron + refresh interval, dashboard refresh, admin bootstrap key, and rate-limit
settings. Nothing is hardcoded.
```

## Architecture
```
            ┌──────────────┐     live HTTP      ┌────────────────┐
            │  Scheduler   │ ─────────────────▶ │   Open-Meteo   │
            │ (APScheduler)│                    │  (3 endpoints) │
            └──────┬───────┘                    └────────────────┘
                   │ run_simulation
                   ▼
  fetch → raw_weather_response → normalize (96 blocks) → solar/wind/hybrid engines
                   │                                          │
                   ▼                                          ▼
            weather_block                            generation_block + daily_summary
                                                            │
                   ┌────────────────────────────────────────┤
                   ▼                                          ▼
            FastAPI (8 secured APIs)                    Dashboard (Chart.js)
```
