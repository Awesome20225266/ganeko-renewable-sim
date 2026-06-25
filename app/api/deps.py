"""API-key authentication, scope enforcement, and per-key rate limiting."""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select

from app.config.settings import get_settings
from app.db.base import get_session
from app.db.models import ApiKey
from app.security import hash_key

# In-process sliding-window rate limiter. (For multi-process deployments use a
# shared store such as Redis; documented in the README.)
_WINDOW_SECONDS = 60.0
_hits: dict[int, deque[float]] = defaultdict(deque)
_lock = threading.Lock()


def _rate_limit_ok(key_id: int, limit: int) -> bool:
    now = time.monotonic()
    with _lock:
        dq = _hits[key_id]
        while dq and dq[0] <= now - _WINDOW_SECONDS:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True


class AuthContext:
    def __init__(self, api_key: ApiKey):
        self.api_key_id = api_key.id
        self.key_prefix = api_key.key_prefix
        self.team = api_key.team
        self.scope = api_key.scope


def _authenticate(request: Request, raw_key: str | None) -> ApiKey:
    settings = get_settings()
    if not raw_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Missing API key (header '{settings.API_KEY_HEADER}').",
        )
    db = get_session()
    try:
        api_key = db.scalar(select(ApiKey).where(ApiKey.key_hash == hash_key(raw_key)))
        if api_key is None or not api_key.is_active:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or inactive API key."
            )
        if api_key.expires_at is not None:
            exp = api_key.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < datetime.now(timezone.utc):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="API key expired."
                )
        limit = api_key.rate_limit_per_min or settings.DEFAULT_RATE_LIMIT_PER_MIN
        if not _rate_limit_ok(api_key.id, limit):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded ({limit}/min).",
            )
        api_key.last_used_at = datetime.now(timezone.utc)
        db.commit()
        # Stash for usage-logging middleware.
        request.state.api_key_id = api_key.id
        request.state.key_prefix = api_key.key_prefix
        # Detach a lightweight copy before the session closes.
        ctx = ApiKey(
            id=api_key.id,
            team=api_key.team,
            name=api_key.name,
            key_prefix=api_key.key_prefix,
            key_hash=api_key.key_hash,
            scope=api_key.scope,
            is_active=api_key.is_active,
            rate_limit_per_min=api_key.rate_limit_per_min,
        )
        return ctx
    finally:
        db.close()


def require_read(
    request: Request,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
) -> AuthContext:
    """Any valid active key (read or admin). Honors the configured header name."""
    settings = get_settings()
    # Prefer the configured header name; fall back to the documented X-API-Key.
    raw_key = request.headers.get(settings.API_KEY_HEADER) or x_api_key
    api_key = _authenticate(request, raw_key)
    return AuthContext(api_key)


def require_admin(ctx: AuthContext = Depends(require_read)) -> AuthContext:
    """Admin-scope keys only."""
    if ctx.scope != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin scope required for this endpoint.",
        )
    return ctx
