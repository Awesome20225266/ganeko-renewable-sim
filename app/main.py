"""FastAPI application entrypoint."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from app.api.routes_admin import router as admin_router
from app.api.routes_plants import router as plants_router
from app.config.settings import get_settings
from app.dashboard import router as dashboard_router
from app.db.base import init_db, session_scope
from app.db.models import ApiUsageLog
from app.logging_conf import configure_logging, get_logger
from app.wrapper.router import router as wrapper_router

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    init_db()  # bootstrap tables (no-op if Alembic already ran)
    scheduler = None
    if settings.SCHEDULER_ENABLED:
        from app.scheduler.service import get_scheduler

        scheduler = get_scheduler()
        try:
            scheduler.start()
        except Exception as exc:  # noqa: BLE001
            logger.error("Scheduler failed to start: %s", exc)
    logger.info("API ready (scheduler_enabled=%s)", settings.SCHEDULER_ENABLED)
    yield
    if scheduler is not None:
        scheduler.shutdown()


app = FastAPI(
    title="Renewable Generation Simulation Platform",
    version="1.0.0",
    description=(
        "Solar / Wind / Hybrid generation simulation from live Open-Meteo weather. "
        "All /plants and /admin endpoints require an API key in the configured header "
        "(default 'X-API-Key'). Responses carry a data label "
        "(HISTORICAL_SIMULATED / LIVE_ESTIMATED / FORECAST_SIMULATED / REPROCESSED / "
        "PARTIAL / FAILED / FINALIZED)."
    ),
    lifespan=lifespan,
)

_origins = [o.strip() for o in get_settings().CORS_ALLOW_ORIGINS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def usage_logging(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    # Log only business/admin API calls (skip dashboard assets / docs / health).
    if path.startswith("/plants") or path.startswith("/admin"):
        try:
            with session_scope() as db:
                db.add(
                    ApiUsageLog(
                        api_key_id=getattr(request.state, "api_key_id", None),
                        key_prefix=getattr(request.state, "key_prefix", None),
                        path=path,
                        method=request.method,
                        status_code=response.status_code,
                        client_host=request.client.host if request.client else None,
                        ts=datetime.now(UTC),
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("usage log failed: %s", exc)
    return response


app.include_router(plants_router)
app.include_router(admin_router)
app.include_router(dashboard_router)
app.include_router(wrapper_router)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/dashboard")


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok", "service": "renewable-sim", "version": "1.0.0"}
