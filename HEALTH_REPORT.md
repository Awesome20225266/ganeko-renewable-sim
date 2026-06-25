# HEALTH REPORT — Renewable Generation Simulation Platform

Generated during the final validation phase. Two layers: **(A) system health** and
**(B) simulation realism**. Both pass. Reproduce with:
```
python -m pytest -q
python -m ruff check app tests
python scripts/realism_check.py
```

---

## (A) System health

### Automated tests & lint
- `pytest` — **30 passed** (engines, normalization, data-quality, API auth).
- `ruff check app tests` — **All checks passed**.

### API endpoints (live server, valid vs invalid key)
Smoke test against a running server (`uvicorn app.main:app`):

| Endpoint | No/invalid key | Valid key |
|---|---|---|
| `GET /health` | 200 (open) | — |
| `GET /plants/{c}/config` | **401** | **200** |
| `GET /plants/{c}/historical?date=` | 401 | **200** |
| `GET /plants/{c}/live` | 401 | **200** |
| `GET /plants/{c}/forecast?date=` | 401 | **200** |
| `GET /plants/{c}/summary?date=` | 401 | **200** |
| `GET /plants/{c}/range?start=&end=` | 401 | **200** |
| `POST /admin/reprocess` | 401 | **200** (admin) / **403** (read key) |
| `POST/GET/DELETE/rotate /admin/api-keys` | 401 | **200** (admin) / **403** (read key) |
| `GET /dashboard` + data feeds | 200 (open, read-only, no secrets) | — |

- Invalid/missing key → **401**; read-scoped key on an admin route → **403**. ✔
- Responses carry data labels: `LIVE_ESTIMATED` for completed blocks, `FORECAST_SIMULATED`
  for today's remaining blocks, `HISTORICAL_SIMULATED` for past, `REPROCESSED` after reprocess. ✔
- No internal IDs / paths / hashes exposed in responses. ✔
- Rate limiting per key (429) and `api_usage_log` records every `/plants` & `/admin` call. ✔

### Scheduler
- In-process scheduler starts on app boot: `Scheduler started (daily=00:30 Asia/Kolkata,
  live every 15min)` / `API ready (scheduler_enabled=True)`. ✔
- Manual triggers work for any date: `app.cli run-daily`, `app.cli live-refresh`,
  `app.cli simulate --date … --mode …`, `app.cli reprocess --dates …`. ✔
- Standalone scheduler process (`app.scheduler_runner`) used by the docker `scheduler` service. ✔

### Dashboard
- Loads in the browser; solar/wind/hybrid 15-min curves render. ✔
- Visually distinguishes **completed** (solid) vs **forecast** (dashed) vs **interpolated**
  (triangle markers) blocks; data-quality badge; weather-variables and previous-days panels. ✔
- Verified live (Jaisalmer plant): solar bell curve zero at night, ~120 MW midday peak;
  wind varies block-to-block; hybrid = solar + wind.

### Data-quality assertions (enforced + tested)
Exactly 96 blocks, no duplicate/missing timestamps, no negatives, solar = 0 at night,
`solar_mw ≤ 160`, `wind_mw ≤ 135`, `total = solar + wind`, CUF ≤ 100%. Failing checks mark
the run `PARTIAL`/`FAILED` and are logged. ✔

### Acceptance criteria
| # | Criterion | Status |
|---|---|---|
| 1 | Clone → `.env` → one command brings up api/scheduler/db/dashboard, no code edits | ✔ (docker compose / local) |
| 2 | Real Open-Meteo data → 96 blocks for past/today/future | ✔ (verified live) |
| 3 | 8 APIs work, key-protected, labelled, reject unauthorized | ✔ |
| 4 | Daily scheduler runs automatically + manual for any date | ✔ |
| 5 | Dashboard shows today's curves; live/forecast distinguished | ✔ |
| 6 | Tests pass incl. data-quality | ✔ (30 passed) |
| 7 | README: zero-to-running < 10 min | ✔ |

---

## (B) Simulation realism

Site: **HYBRID01** (Jaisalmer, Rajasthan; 26.9124, 70.9026; Asia/Kolkata) — Solar 160 MW AC /
240 MW DC, Wind 135 MW. Live Open-Meteo data. Full check: `python scripts/realism_check.py`
→ **REALISM VALIDATION: PASS**.

### Per-day physical checks (sampled days)
| Day | Mode | Solar MWh | Wind MWh | Solar CUF | Wind CUF | Hybrid CUF | Specific yield |
|---|---|---|---|---|---|---|---|
| 2025-06-21 | HISTORICAL | 694.3 | 925.8 | 18.1% | 28.6% | 22.9% | 2.89 kWh/kWp |
| 2025-12-21 | HISTORICAL | 353.8 | 57.0 | 9.2% | 1.8% | 5.8% | 1.47 kWh/kWp |
| today (LIVE) | LIVE | 1008.9 | 482.0 | 26.3% | 14.9% | 21.1% | 4.20 kWh/kWp |
| today+3 (FORECAST) | FORECAST | 1008.7 | 834.9 | 26.3% | 25.8% | 26.0% | 4.20 kWh/kWp |

Every sampled day passes: 96 blocks, **solar exactly zero at night**, solar peaks near local
solar noon (blocks ~45–53 ≈ 11:00–13:00), no negatives, solar ≤ 160 MW, wind ≤ 135 MW,
totals reconcile, hybrid CUF ≤ 100%, and **wind is zero below cut-in / above cut-out** (power-curve respected).

### Cross-checks
- **Seasonal:** summer solar (694 MWh, 21-Jun) **>** winter solar (354 MWh, 21-Dec). ✔
  Winter shows fewer daylight blocks (56 night vs 42) and a later/flatter peak — physically correct.
- **Solar CUF band:** 9–26% across season/weather — within the plausible 6–32% envelope. ✔
- **Clear-day specific yield:** clearest sampled day **4.20 kWh/kWp/day**, inside the
  expected ~3.5–5.5 band for a good site. (An arbitrary calendar date can be cloudy — e.g.
  pre-monsoon 21-Jun at 2.89 — which is physically valid, not a defect.) ✔
- **Inverter clipping:** appears only when derated DC would exceed 160 MW AC (cool, high-irradiance
  conditions), per `test_solar_clips_at_ac_capacity` / `test_solar_no_clip_at_warm_stc`. ✔
- **Wind tracks weather:** wind generation rises and falls with block-to-block wind-speed
  variation and follows the configured power curve.

### Regression guard
A **golden reference day** (`golden_reference_day.json`, 2025-06-21) stores the full 96-block
solar/wind profile + daily summary so any later formula/assumption change can be diff-checked.

### Unphysical-output scan (all clear)
No solar at midnight, no CUF > 100%, no flat/constant curves, no wind ignoring the power
curve, no totals that fail to reconcile.

---

**Conclusion:** all acceptance criteria met; system health and simulation realism both pass.
`BUILD_COMPLETE` marker written.
