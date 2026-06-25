#!/usr/bin/env bash
# Container entrypoint: wait for DB, run migrations, seed (idempotent), then exec the role.
set -euo pipefail

ROLE="${1:-api}"

# Wait for Postgres if a postgres URL is configured.
if [[ "${DATABASE_URL:-}" == postgresql* ]]; then
  echo "[entrypoint] waiting for database..."
  python - <<'PY'
import os, time, sys
from sqlalchemy import create_engine, text
url = os.environ["DATABASE_URL"]
for i in range(60):
    try:
        create_engine(url).connect().execute(text("select 1"))
        print("[entrypoint] database is up"); sys.exit(0)
    except Exception as e:
        print(f"[entrypoint] db not ready ({i}): {e}"); time.sleep(2)
sys.exit("[entrypoint] database never became ready")
PY
fi

# Migrations + idempotent seed run once per container start; safe to repeat.
echo "[entrypoint] running migrations"
alembic upgrade head
echo "[entrypoint] seeding (idempotent)"
python -m app.db.seed || true

case "$ROLE" in
  api)
    echo "[entrypoint] starting API (uvicorn)"
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000
    ;;
  scheduler)
    echo "[entrypoint] starting scheduler"
    exec python -m app.scheduler_runner
    ;;
  *)
    echo "[entrypoint] unknown role: $ROLE"; exit 1
    ;;
esac
