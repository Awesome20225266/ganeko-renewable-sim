"""Shared business operations used by both the secured API and the dashboard console.

Keeping these here lets the key-protected `/plants` & `/admin` endpoints and the
trusted same-origin dashboard endpoints share one implementation.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import ApiKey, Plant, PlantConfig
from app.security import generate_api_key, hash_key, key_prefix
from app.simulate import load_active_config

_CARRY_EXCLUDE = (
    "id",
    "plant_id",
    "config_version",
    "is_active",
    "created_at",
    # Never inherited: an effective date belongs to the change that set it. Carrying
    # it forward would silently re-date every later edit to an old cutover.
    "effective_from_date",
)


def create_config_version(db: Session, code: str, fields: dict) -> PlantConfig:
    """Create a new active plant-config version from the current one + overrides.

    History is preserved: the previous active config is deactivated, not deleted.
    Raises ValueError if the plant is unknown.
    """
    cur = load_active_config(db, code)
    plant = db.scalar(select(Plant).where(Plant.plant_code == code))
    # Number from the highest version that EXISTS, not the highest still active.
    # config_version is unique per plant, so deriving it from the active config meant
    # that deactivating a version (or any version existing above the active one) made
    # the next save collide on uq_plant_config_version and fail with a 500.
    highest = db.scalar(
        select(func.max(PlantConfig.config_version)).where(PlantConfig.plant_code == code)
    )
    new_version = (highest or cur.config_version) + 1

    carried = {
        c.name: getattr(cur, c.name)
        for c in PlantConfig.__table__.columns
        if c.name not in _CARRY_EXCLUDE
    }
    carried.update({k: v for k, v in fields.items() if v is not None})
    # Keep DC/AC ratio consistent when capacities change.
    if carried.get("solar_ac_mw"):
        carried["dc_ac_ratio"] = round(carried["solar_dc_mw"] / carried["solar_ac_mw"], 4)

    # A config with no effective date supersedes everything, so the previous versions
    # are retired as before. A FORWARD-DATED config does not: earlier dates are still
    # served by the version they were published under, so that version must stay
    # active or `load_config_for_date` would find nothing for them.
    if carried.get("effective_from_date") is None:
        db.query(PlantConfig).filter(
            PlantConfig.plant_code == code, PlantConfig.is_active.is_(True)
        ).update({PlantConfig.is_active: False}, synchronize_session=False)

    new_cfg = PlantConfig(
        plant_id=plant.id, config_version=new_version, is_active=True, **carried
    )
    db.add(new_cfg)
    if plant:
        plant.active_config_version = new_version
    db.flush()
    return new_cfg


def create_api_key(
    db: Session,
    team: str,
    name: str,
    scope: str = "read",
    rate_limit_per_min: int = 120,
    expires_in_days: int | None = None,
) -> tuple[str, ApiKey]:
    """Mint an API key. Returns (raw_key_shown_once, row). Key is hashed at rest."""
    raw = generate_api_key("rk" if scope == "read" else "admin")
    expires_at = None
    if expires_in_days:
        expires_at = datetime.now(UTC) + timedelta(days=expires_in_days)
    row = ApiKey(
        team=team,
        name=name,
        key_prefix=key_prefix(raw),
        key_hash=hash_key(raw),
        scope=scope,
        is_active=True,
        rate_limit_per_min=rate_limit_per_min,
        expires_at=expires_at,
    )
    db.add(row)
    db.flush()
    return raw, row


def revoke_api_key(db: Session, prefix: str) -> int:
    """Deactivate keys by prefix; refuses to revoke the last active admin key.

    Returns the number of keys deactivated. Raises ValueError on guard violations.
    """
    rows = list(db.scalars(select(ApiKey).where(ApiKey.key_prefix == prefix)))
    if not rows:
        raise ValueError(f"No key with prefix '{prefix}'")
    active = [k for k in rows if k.is_active]
    if not active:
        return 0
    if any(k.scope == "admin" for k in active):
        remaining = db.scalar(
            select(ApiKey).where(
                ApiKey.scope == "admin",
                ApiKey.is_active.is_(True),
                ApiKey.key_prefix != prefix,
            )
        )
        if remaining is None:
            raise ValueError("Refusing to revoke the last active admin key.")
    for k in active:
        k.is_active = False
    return len(active)
