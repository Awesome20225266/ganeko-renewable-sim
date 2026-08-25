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

## Actuals are immutable

An Actual is **written once and never revised**. Once a 15-minute block has been
published as `LIVE_ESTIMATED` or `HISTORICAL_SIMULATED`, re-reading it returns the
same number forever — even though the weather provider keeps revising its own view
of the past (measured 2026-08-25: already-elapsed intervals came back 19–56%
brighter on a later model run, which used to restate completed blocks mid-day).

Every block carries **`is_final`**:

| `is_final` | Meaning |
|---|---|
| `true` | Settled Actual. Safe to store, reconcile and act on; it will never change. |
| `false` | Not an Actual yet — a forecast block, still expected to move. (Historical Actuals always report `true`.) |

`/today-completed-blocks` also returns **`as_of`** — the vintage of the weather
behind the not-yet-final part of the response — so a polling client can tell one
refresh from the next. `/current` already carried this field.

**The same interval keeps one value for its whole life.** A block you read today
from `/today-completed-blocks` returns that exact number tomorrow from
`/historical` — the two endpoints never disagree about the same 15 minutes, so you
can reconcile across them without allowing for drift.

A published Actual has **no override path**. Admin reprocess recomputes a date into
a separate diagnostic record that is never served, so nothing — no read, no refresh,
no admin action — changes a number you have already been given.

`/schedule` is different by design: it is a forward commitment, so it is revised
automatically as better forecasts arrive — but only **beyond a 2-hour operational
lock**. The block you are inside and the next two hours are committed dispatch and
cannot move; the first block a revision may touch is the one starting 2h15m after the
current block began. It never rewrites a block already past, nor any schedule for a
date that has finished.

## Configuration (environment variables)

```
RENEWABLE_PLANT_ID=HYBRID01
RENEWABLE_PLANT_TZ=Asia/Kolkata
RENEWABLE_WRAPPER_USER_API_KEY=<key your users send>   # optional; blank = open
GENERATION_IMMUTABILITY_CUTOVER=2026-08-25T15:00:00+05:30   # when enforcement starts
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
| `GET /api/renewable/schedule?date=YYYY-MM-DD` | Day-ahead **P90 schedule** — solar, wind and total, 96 blocks. Serves up to **tomorrow (X+1)**; carries its own `data_policy` of `DAY_AHEAD_SCHEDULE_UPTO_X_PLUS_1` |

> **Why `/schedule` is the one route that looks forward.** A schedule is
> published *before* the day it describes — that is what makes it a schedule, so
> X+1 is the point of it. Every actual-data route stays capped at today, because
> actual generation cannot exist for a future date. Beyond X+1 is refused: those
> schedules were anchored on a multi-day-out forecast, and serving them would
> present a stale anchor as if it were day-ahead.

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

# Day-ahead P90 schedule (solar + wind + total) — date must not be in the future
curl -H "X-API-Key: <USER_WRAPPER_KEY>" "http://localhost:8000/api/renewable/schedule?date=2026-06-25"

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
