"""API-key generation, hashing and verification. Keys are never stored in plaintext."""
from __future__ import annotations

import hashlib
import secrets


def generate_api_key(prefix: str = "rk") -> str:
    """Generate a new opaque API key: '<prefix>_<43 url-safe chars>'."""
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def hash_key(raw_key: str) -> str:
    """Return the SHA-256 hex digest used for at-rest storage and lookup."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def key_prefix(raw_key: str, length: int = 12) -> str:
    """Short, non-secret prefix for display/logging (never the full key)."""
    return raw_key[:length]
