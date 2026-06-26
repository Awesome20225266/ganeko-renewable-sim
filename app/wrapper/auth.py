"""Optional, env-based API key for the user-facing wrapper.

This key (RENEWABLE_WRAPPER_USER_API_KEY) is what external users/Excel send as the
X-API-Key header. The wrapper reads data in-process, so this is the only key involved.
If the env var is unset, the wrapper is open (auth disabled) — set it to lock down.
"""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from app.config.settings import get_settings


def require_wrapper_user(x_api_key: str | None = Header(None, alias="X-API-Key")) -> None:
    """Validate the wrapper user key when one is configured (timing-safe compare)."""
    configured = get_settings().RENEWABLE_WRAPPER_USER_API_KEY
    if not configured:
        return  # optional auth disabled — wrapper is open
    if not x_api_key or not hmac.compare_digest(x_api_key, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
        )
