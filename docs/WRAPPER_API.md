# Restricted Renewable Wrapper API (`/api/renewable/*`)

A locked-down, user-facing API that exposes **only live and historical** generation
data — **never forecast**. It reads this application's own data **in-process** (no
outbound call, no provider key), so there is nothing to activate beyond an optional
shared user key, and external users/Excel never see raw provider paths or internals.

## What it allows / blocks

| Allowed | Blocked |
|---|---|
| `LIVE_ESTIMATED` (today, completed) | `FORECAST_SIMULATED` (any forecast) |
| `HISTORICAL_SIMULATED` (past days) | future dates, future blocks, the `/forecast` endpoint |

Forecast is blocked in three layers: (1) no forecast route exists and future dates are
rejected at validation, (2) any `FORECAST_SIMULATED` block/summary is stripped from
responses, (3) `/current` returns **403** if its reading is a forecast.

## Configuration (environment variables)

```
RENEWABLE_PLANT_ID=HYBRID01
RENEWABLE_PLANT_TZ=Asia/Kolkata
RENEWABLE_WRAPPER_USER_API_KEY=<key your users send>   # optional; blank = open
```

- The wrapper reads data **in-process** — there is **no provider key** to set or maintain.
- `RENEWABLE_WRAPPER_USER_API_KEY` is what **your users** send as `X-API-Key`. Set it to
  one value you hand to your users before sharing. If left blank, the wrapper is open.

## Endpoints

All requests send the **wrapper user key** (not the provider key):
`X-API-Key: <USER_WRAPPER_KEY>`

| Endpoint | Purpose |
|---|---|
| `GET /api/renewable/current` | Latest real-time live reading (poll this) |
| `GET /api/renewable/today-completed-blocks` | Today's completed live blocks only |
| `GET /api/renewable/historical?date=YYYY-MM-DD` | A completed past day |
| `GET /api/renewable/range?start=YYYY-MM-DD&end=YYYY-MM-DD` | Block range (≤ 31 days) |
| `GET /api/renewable/summary?date=YYYY-MM-DD` | Daily totals (single date) |
| `GET /api/renewable/summary?start=…&end=…` | Daily totals (range) |

Add `&format=csv` to `current`, `today-completed-blocks`, `historical`, and `range`
for flat, Excel-friendly CSV.

### curl examples

```bash
# Real-time polling
curl -H "X-API-Key: <USER_WRAPPER_KEY>" http://localhost:8000/api/renewable/current

# Today's completed (live) blocks
curl -H "X-API-Key: <USER_WRAPPER_KEY>" http://localhost:8000/api/renewable/today-completed-blocks

# Historical day
curl -H "X-API-Key: <USER_WRAPPER_KEY>" "http://localhost:8000/api/renewable/historical?date=2026-06-21"

# Block range
curl -H "X-API-Key: <USER_WRAPPER_KEY>" "http://localhost:8000/api/renewable/range?start=2026-06-21&end=2026-06-26"

# CSV for Excel
curl -H "X-API-Key: <USER_WRAPPER_KEY>" "http://localhost:8000/api/renewable/today-completed-blocks?format=csv"
```

## Excel (Power Query) note

Point Excel at **the wrapper** (`/api/renewable/*`), **not** the external
`renewable-sim.onrender.com` API. Data → Get Data → From Web → enter the wrapper URL,
and add a header `X-API-Key = <USER_WRAPPER_KEY>` (or use the `?format=csv` URL for the
simplest flat import). This keeps the provider key server-side and guarantees no
forecast data reaches the spreadsheet.

## Real-time polling script

```bash
# Talks to the wrapper only; reads the wrapper user key from the environment.
export WRAPPER_BASE_URL=http://localhost:8000
export RENEWABLE_WRAPPER_USER_API_KEY=<USER_WRAPPER_KEY>
python scripts/poll_renewable_current.py --poll-seconds 60
```

It prints each new block (plant_id, block_no, block_start, solar/wind/total MW,
data_label, client time) and, when the block hasn't advanced, prints
`No new block yet. Latest block is <block_start>. Checking again in <n> seconds...`.
It never prints any API key.

## Errors (clean, no internals leaked)

| Status | Meaning |
|---|---|
| `400` | Invalid request / future date not allowed / range too large |
| `401` | Missing or invalid wrapper user key |
| `403` | Forecast data is not allowed |
| `429` | Rate limit exceeded |
| `500` | Server not configured (missing `RENEWABLE_API_KEY`) |
| `502` | Renewable data provider unavailable |
