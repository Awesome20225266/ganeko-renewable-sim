# Deployment Guide

How to take this from local to a server other people can reach. Pick **one** of the
options below. All of them boil down to: run the container(s), point them at a Postgres
database, set a few environment variables, and put HTTPS in front.

---

## 0. Pre-flight (do this for every option)

1. **Use Postgres, not SQLite**, for anything shared/persistent:
   ```
   DATABASE_URL=postgresql+psycopg2://USER:PASSWORD@HOST:5432/renewable
   ```
2. **Set a real admin key** (don't ship the default):
   ```
   ADMIN_BOOTSTRAP_KEY=<long-random-string>     # e.g. `openssl rand -hex 24`
   ```
3. **Lock down CORS** to the front-ends that should call the API:
   ```
   CORS_ALLOW_ORIGINS=https://your-dashboard.example.com
   ```
4. **Decide who runs the scheduler.** Exactly one process should have
   `SCHEDULER_ENABLED=true` (the dedicated `scheduler` service). API/dashboard instances
   run with `SCHEDULER_ENABLED=false` so jobs don't run multiple times.
5. The container entrypoint runs `alembic upgrade head` + idempotent seed on start, so a
   fresh database is provisioned automatically.

Generate a strong key quickly:
```bash
python -c "import secrets; print('admin_'+secrets.token_urlsafe(32))"
```

---

## Option A — Docker on a VM (recommended, full control)

Works on any Ubuntu/Debian VM (AWS EC2, GCP, Azure, DigitalOcean, Hetzner…).

```bash
# 1. Install Docker + compose plugin, then:
git clone <repo> && cd "API Creation"
cp .env.example .env
#   edit .env: ADMIN_BOOTSTRAP_KEY, CORS_ALLOW_ORIGINS, PLANT_LAT/LON/TZ, capacities

# 2. Bring up db + api + scheduler + dashboard
docker compose up -d --build

# 3. Verify
curl http://localhost:8000/health
```
- `docker-compose.yml` already wires Postgres + `api` (8000) + `scheduler` + `dashboard` (8001).
- Data persists in the `pgdata` volume.

### HTTPS / reverse proxy
Put Caddy or Nginx in front (TLS termination + a public hostname). Caddy is the least
work — one file gives you automatic Let's Encrypt certificates:
```
# Caddyfile
api.example.com {
    reverse_proxy localhost:8000
}
```
```bash
caddy run --config Caddyfile
```
Then your shareable base URL is `https://api.example.com` and the dashboard is at
`https://api.example.com/dashboard`. (The "API share file" downloaded from the dashboard
uses whatever origin you open it on, so it will contain the public URL automatically.)

---

## Option B0 — Render in ~10 min using the bundled `render.yaml` (recommended)

This repo ships a ready Blueprint (`render.yaml`): one free Docker web service (API +
dashboard + in-process scheduler) + a free Postgres database. Migrations and the seed run
automatically on boot.

1. **Put the code on GitHub** (Render deploys from a Git repo). From the project folder:
   ```bash
   git init -b main            # if not already a repo
   git add -A && git commit -m "Renewable simulation platform"
   # create an EMPTY repo on github.com first, then:
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
2. Go to **https://dashboard.render.com → New + → Blueprint**.
3. Connect your GitHub and pick the repo. Render reads `render.yaml` and shows a web
   service + a Postgres database — click **Apply**.
4. Wait for the build (a few minutes). Render gives you a public HTTPS URL like
   `https://renewable-sim.onrender.com`.
5. Open `…/dashboard`. The admin key is auto-generated (`ADMIN_BOOTSTRAP_KEY`); find it
   under the service's **Environment** tab if you need it for the key-protected APIs.

Free-tier caveats: the web service sleeps after ~15 min idle (first request then ~30s,
and the in-process scheduler pauses while asleep); free Postgres is time-limited. For
always-on + a dedicated scheduler worker, switch to a paid plan and add a `type: worker`
service running `bash scripts/entrypoint.sh scheduler` (see the comment in `render.yaml`).

> Railway/Fly use the same Docker image — see Option B below for the per-platform recipe.

## Option B — PaaS (Render / Railway / Fly.io)

The same Docker image deploys to any container PaaS. General recipe:

1. Create a **managed Postgres** instance; copy its connection string into `DATABASE_URL`
   (use the `postgresql+psycopg2://` prefix).
2. Create **two services from this repo's Dockerfile**:
   - **web** — command `api` (default), expose port 8000, `SCHEDULER_ENABLED=false`.
   - **worker** — command `scheduler`, no public port, `SCHEDULER_ENABLED=true`.
3. Set env vars on both: `DATABASE_URL`, `ADMIN_BOOTSTRAP_KEY`, `CORS_ALLOW_ORIGINS`,
   `PLANT_*`, capacities.
4. The PaaS provides HTTPS + a public hostname automatically.

Platform notes:
- **Render:** Blueprint = one Web Service (`docker run … api`) + one Background Worker
  (`… scheduler`) + a Postgres add-on. Health check path `/health`.
- **Railway:** add the repo, set the start command per service (`bash scripts/entrypoint.sh api`
  / `… scheduler`), attach the Postgres plugin (it injects `DATABASE_URL`).
- **Fly.io:** `fly launch` (it detects the Dockerfile), `fly postgres create` + `fly postgres
  attach`, and add a `[processes]` block: `app = "bash scripts/entrypoint.sh api"` and
  `worker = "bash scripts/entrypoint.sh scheduler"`.

> If you'd like, I can commit a ready-made `render.yaml` / `railway.json` / `fly.toml` —
> ask and I'll add the one you want.

---

## Option C — Single container (smallest, no separate worker)

For a low-traffic deployment you can run one container that serves the API/dashboard **and**
runs the scheduler in-process:
```bash
docker run -d -p 8000:8000 \
  -e DATABASE_URL=postgresql+psycopg2://… \
  -e ADMIN_BOOTSTRAP_KEY=… \
  -e SCHEDULER_ENABLED=true \
  -e CORS_ALLOW_ORIGINS=https://your-frontend \
  <image> api
```
Trade-off: don't scale this service to >1 replica, or the scheduler runs N times.

---

## Option D — instant public URL (Cloudflare quick tunnel, no account)

Make the server running on your machine reachable on the internet in ~1 minute — handy
for a quick share/demo. The URL is live only while your PC + the tunnel run, and changes
on restart.
```powershell
py -m uvicorn app.main:app --port 8000      # terminal 1 (the app)
powershell -File scripts/tunnel.ps1          # terminal 2 → prints https://<x>.trycloudflare.com
```
The helper downloads `cloudflared` on first use and points it at `127.0.0.1:8000`
(using `127.0.0.1`, not `localhost`, avoids an IPv6 `::1` resolution issue that makes
some routes 404). Security: this exposes the keyless console publicly — set
`DASHBOARD_CONSOLE_WRITE=false` first, or only share with people you trust.

## Scaling & production notes

- **Rate limiter** is in-process (per replica). For multiple API replicas, move it to a
  shared store (Redis) so limits are global. Until then, run a single API replica or accept
  per-replica limits. Auth, usage logging and all data are already DB-backed and scale fine.
- **Database connection pool:** SQLAlchemy defaults are fine for a few replicas; tune
  `pool_size` if you scale out.
- **Open-Meteo limits:** the free tier is generous but rate-limited; the client already
  retries with backoff on 429. For heavy multi-plant use, consider their commercial tier.
- **Backups:** back up the Postgres volume/instance. Raw weather responses + versioned
  outputs are all stored, so simulations are fully reproducible.
- **Secrets:** never commit `.env`. Inject env vars via your platform's secret manager.

## Security checklist before going public
- [ ] `ADMIN_BOOTSTRAP_KEY` rotated to a strong random value (rotate again via
      `POST /admin/api-keys/{prefix}/rotate`).
- [ ] `CORS_ALLOW_ORIGINS` restricted to known front-ends (not `*`).
- [ ] HTTPS enforced (reverse proxy / PaaS).
- [ ] Issue **read-scoped** keys to consumers; keep admin keys internal.
- [ ] Postgres not publicly exposed (or firewalled to the app only).
- [ ] **Dashboard console is unauthenticated by design** — its write actions (generate
      simulations, edit config, mint/revoke API keys) need no key. For a public deployment
      either (a) put `/dashboard` behind your own network/SSO/proxy, or (b) set
      `DASHBOARD_CONSOLE_WRITE=false` so the console becomes read-only and all changes must
      go through the key-protected `/plants` & `/admin` APIs.
- [ ] Read-only dashboard feeds expose no secrets/IDs; if the generation data itself is
      sensitive, gate the whole dashboard behind your auth/proxy.
```
