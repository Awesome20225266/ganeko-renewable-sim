"""Admin-only endpoints (7-8): reprocess and API-key management."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select

from app.api.deps import AuthContext, require_admin
from app.api.schemas import (
    CreateKeyRequest,
    CreateKeyResponse,
    KeyInfoOut,
    KeyListOut,
    MessageOut,
    RefreshForecastRequest,
    RefreshForecastResponse,
    RefreshForecastResultItem,
    ReprocessRequest,
    ReprocessResponse,
    ReprocessResultItem,
)
from app.db.base import session_scope
from app.db.models import ApiKey
from app.security import generate_api_key, hash_key, key_prefix
from app.simulate import load_active_config, run_simulation
from app.weather.client import DataMode

router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/reprocess", response_model=ReprocessResponse)
async def reprocess(req: ReprocessRequest, ctx: AuthContext = Depends(require_admin)):
    """Endpoint 7: re-run simulation for selected dates (new versioned outputs)."""
    mode = None
    if req.mode:
        try:
            mode = DataMode(req.mode.upper())
        except ValueError:
            raise HTTPException(400, f"Invalid mode '{req.mode}'") from None

    results: list[ReprocessResultItem] = []
    for d in req.dates:
        summary = await run_simulation(
            req.plant_code, d, mode, triggered_by="reprocess", force_refetch=True
        )
        results.append(
            ReprocessResultItem(
                sim_date=d,
                mode=summary.mode,
                status=summary.status,
                data_label=summary.data_label,
                blocks_written=summary.blocks_written,
                quality_status=summary.quality_status,
                total_mwh=round(summary.total_mwh, 3),
                issues=summary.issues,
            )
        )
    return ReprocessResponse(
        plant_code=req.plant_code, triggered=len(results), results=results
    )


@router.post("/refresh-forecast", response_model=RefreshForecastResponse)
async def refresh_forecast(
    req: RefreshForecastRequest, ctx: AuthContext = Depends(require_admin)
):
    """Re-run FORECAST + re-issue the day-ahead P90 schedule for FUTURE dates.

    The scheduler skips a forecast that is still fresh, so after a config change the
    already-computed day-ahead would otherwise coast on the old assumptions until it
    aged out. This forces it, and only forward.
    """
    from app.schedule import ensure_schedule, get_schedule

    with session_scope() as db:
        tz = load_active_config(db, req.plant_code).timezone
    today = datetime.now(ZoneInfo(tz)).date()
    not_future = sorted({d.isoformat() for d in req.dates if d <= today})
    if not_future:
        raise HTTPException(
            400,
            f"Future dates only (plant-local today is {today}). "
            f"Rejected: {', '.join(not_future)}. Today's and past generation is "
            f"published data — use /admin/reprocess for a diagnostic re-run.",
        )

    results: list[RefreshForecastResultItem] = []
    for d in sorted(set(req.dates)):
        summary = await run_simulation(
            req.plant_code, d, DataMode.FORECAST,
            triggered_by="admin-forecast-refresh", force_refetch=True,
        )
        item = RefreshForecastResultItem(
            sim_date=d,
            status=summary.status,
            data_label=summary.data_label,
            blocks_written=summary.blocks_written,
            total_mwh=round(summary.total_mwh, 3),
            issues=summary.issues,
        )
        if req.reissue_schedule and summary.status != "FAILED":
            try:
                await run_in_threadpool(ensure_schedule, req.plant_code, d, force=True)
                with session_scope() as db:
                    rows = get_schedule(db, req.plant_code, d)
                item.schedule_reissued = True
                item.schedule_total_p90_mwh = round(
                    sum(r.total_p90_mwh or 0.0 for r in rows), 3
                )
            except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
                item.issues = [*item.issues, f"schedule re-issue failed: {exc}"]
        results.append(item)

    return RefreshForecastResponse(
        plant_code=req.plant_code, triggered=len(results), results=results
    )


def _key_info(k: ApiKey) -> KeyInfoOut:
    return KeyInfoOut(
        key_prefix=k.key_prefix,
        team=k.team,
        name=k.name,
        scope=k.scope,
        is_active=k.is_active,
        rate_limit_per_min=k.rate_limit_per_min,
        created_at=k.created_at,
        expires_at=k.expires_at,
        last_used_at=k.last_used_at,
    )


@router.post("/api-keys", response_model=CreateKeyResponse)
def create_key(req: CreateKeyRequest, ctx: AuthContext = Depends(require_admin)):
    """Endpoint 8 (create): mint a new team API key (returned once, hashed at rest)."""
    raw = generate_api_key("rk" if req.scope == "read" else "admin")
    expires_at = None
    if req.expires_in_days:
        expires_at = datetime.now(UTC) + timedelta(days=req.expires_in_days)
    with session_scope() as db:
        row = ApiKey(
            team=req.team,
            name=req.name,
            key_prefix=key_prefix(raw),
            key_hash=hash_key(raw),
            scope=req.scope,
            is_active=True,
            rate_limit_per_min=req.rate_limit_per_min,
            expires_at=expires_at,
        )
        db.add(row)
    return CreateKeyResponse(
        api_key=raw,
        key_prefix=key_prefix(raw),
        team=req.team,
        name=req.name,
        scope=req.scope,
        rate_limit_per_min=req.rate_limit_per_min,
        expires_at=expires_at,
    )


@router.get("/api-keys", response_model=KeyListOut)
def list_keys(ctx: AuthContext = Depends(require_admin)):
    """Endpoint 8 (monitor): list keys (no secrets/hashes exposed)."""
    with session_scope() as db:
        rows = list(db.scalars(select(ApiKey).order_by(ApiKey.created_at.desc())))
        return KeyListOut(count=len(rows), keys=[_key_info(k) for k in rows])


@router.delete("/api-keys/{prefix}", response_model=MessageOut)
def revoke_key(prefix: str, ctx: AuthContext = Depends(require_admin)):
    """Endpoint 8 (revoke): deactivate a key by its prefix."""
    with session_scope() as db:
        rows = list(db.scalars(select(ApiKey).where(ApiKey.key_prefix == prefix)))
        if not rows:
            raise HTTPException(404, f"No key with prefix '{prefix}'")
        active = [k for k in rows if k.is_active]
        if not active:
            return MessageOut(message=f"Key '{prefix}' already inactive.")
        # Guard: never revoke the last active admin key.
        if any(k.scope == "admin" for k in active):
            remaining_admins = db.scalar(
                select(ApiKey).where(
                    ApiKey.scope == "admin",
                    ApiKey.is_active.is_(True),
                    ApiKey.key_prefix != prefix,
                )
            )
            if remaining_admins is None:
                raise HTTPException(400, "Refusing to revoke the last active admin key.")
        for k in active:
            k.is_active = False
        return MessageOut(message=f"Revoked {len(active)} key(s) with prefix '{prefix}'.")


@router.post("/api-keys/{prefix}/rotate", response_model=CreateKeyResponse)
def rotate_key(prefix: str, ctx: AuthContext = Depends(require_admin)):
    """Endpoint 8 (rotate): revoke an existing key and issue a replacement."""
    with session_scope() as db:
        existing = db.scalar(
            select(ApiKey).where(ApiKey.key_prefix == prefix, ApiKey.is_active.is_(True))
        )
        if existing is None:
            raise HTTPException(404, f"No active key with prefix '{prefix}'")
        team, name, scope, limit = (
            existing.team,
            existing.name,
            existing.scope,
            existing.rate_limit_per_min,
        )
        raw = generate_api_key("rk" if scope == "read" else "admin")
        existing.is_active = False
        db.add(
            ApiKey(
                team=team,
                name=f"{name} (rotated)",
                key_prefix=key_prefix(raw),
                key_hash=hash_key(raw),
                scope=scope,
                is_active=True,
                rate_limit_per_min=limit,
            )
        )
    return CreateKeyResponse(
        api_key=raw,
        key_prefix=key_prefix(raw),
        team=team,
        name=f"{name} (rotated)",
        scope=scope,
        rate_limit_per_min=limit,
        expires_at=None,
    )
