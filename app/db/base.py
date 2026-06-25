"""SQLAlchemy engine/session management. Works with SQLite and Postgres."""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config.settings import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker | None = None


def _make_engine(url: str):
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        # Ensure the parent directory for a file-based SQLite DB exists.
        if ":///" in url:
            path = url.split(":///", 1)[1]
            if path and path not in (":memory:",):
                parent = os.path.dirname(path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
    return create_engine(url, connect_args=connect_args, future=True, pool_pre_ping=True)


def get_engine():
    global _engine
    if _engine is None:
        _engine = _make_engine(get_settings().DATABASE_URL)
    return _engine


def get_session() -> Session:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(), autoflush=False, autocommit=False, future=True
        )
    return _SessionLocal()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope: commits on success, rolls back on error."""
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """Create all tables (bootstrap fallback when not using Alembic, e.g. SQLite)."""
    # Import models so they register on Base.metadata before create_all.
    from app.db import models  # noqa: F401

    Base.metadata.create_all(bind=get_engine())
