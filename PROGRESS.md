# Build Progress

Human-readable companion to `BUILD_STATE.json`. On resume, **read `BUILD_STATE.json` first** and continue from `next_action`.

## Status: Phase 2 — Open-Meteo client

| Phase | Description | Status |
|---|---|---|
| 1 | Scaffold, config, DB models + migrations, seed plant | ✅ done |
| 2 | Open-Meteo client + raw storage + 96-block normalization | ⏳ pending |
| 3 | Solar / wind / hybrid engines + unit tests | ⏳ pending |
| 4 | Output persistence + daily summaries + versioning | ⏳ pending |
| 5 | FastAPI endpoints + API-key auth + rate limit + logging | ⏳ pending |
| 6 | Scheduler (daily + manual) + data-quality enforcement | ⏳ pending |
| 7 | Dashboard | ⏳ pending |
| 8 | Reprocessing API + admin key management | ⏳ pending |
| 9 | Tests, Docker, README, e2e smoke run | ⏳ pending |
| 10 | Final health + realism validation → `BUILD_COMPLETE` | ⏳ pending |

## Design decisions
- **Site location**: the attached `Book1 (4).xlsx` is a 96-block/day hybrid generation+BESS financial model and contains **no lat/long** (verified: no coordinate text/keywords anywhere in the 35k-row sheet). Per the spec, location is a configurable placeholder. The seed plant defaults to **Jaisalmer, Rajasthan, India** (`26.9124, 70.9026`, `Asia/Kolkata`) — a high solar + high wind region ideal for realism validation. Change it in `.env` (`PLANT_LAT`/`PLANT_LON`/`PLANT_TZ`) or via the DB plant config; no code changes needed.
- **Capacities** (from the workbook): Solar AC 160 MW, Solar DC 240 MW (DC/AC 1.5), Wind AC 135 MW, PPA 140 MW, 15-min blocks, 96/day.
- **DB**: `DATABASE_URL` drives SQLAlchemy. Defaults to SQLite for zero-config local use; docker-compose uses Postgres. Alembic migrations + `create_all` bootstrap fallback.

## Resume protocol
1. Read `BUILD_STATE.json`.
2. If `build_complete` is true and `BUILD_COMPLETE` marker exists → done.
3. Otherwise continue from `next_action`. Every step ends with a state-file update + small git commit.
