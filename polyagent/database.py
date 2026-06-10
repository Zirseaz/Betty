"""Async database engine, session factory, and bootstrap utilities.

Uses SQLAlchemy 2.0 async API backed by ``aiosqlite``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from polyagent.config import get_settings

logger = logging.getLogger(__name__)

# ── Module-level singletons (initialised lazily) ─────────────────
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _ensure_data_dir(url: str) -> None:
    """Create the parent directory for an SQLite database file if needed."""
    if url.startswith("sqlite"):
        # Extract file path from URLs like "sqlite+aiosqlite:///./data/polyagent.db"
        parts = url.split("///", maxsplit=1)
        if len(parts) == 2:
            db_path = Path(parts[1])
            db_path.parent.mkdir(parents=True, exist_ok=True)
            logger.debug("Ensured data directory exists: %s", db_path.parent)


def get_engine() -> AsyncEngine:
    """Return the async SQLAlchemy engine, creating it on first call.

    The engine is configured with sensible defaults for an async SQLite
    backend (single-writer, WAL journal mode via connect args).
    """
    global _engine  # noqa: PLW0603

    if _engine is None:
        settings = get_settings()
        _ensure_data_dir(settings.database_url)

        _engine = create_async_engine(
            settings.database_url,
            echo=False,
            future=True,
            connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},
        )
        
        if "sqlite" in settings.database_url:
            from sqlalchemy import event
            @event.listens_for(_engine.sync_engine, "connect")
            def set_sqlite_pragma(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.close()
                
        logger.info("Async engine created for %s", settings.database_url)

    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the async session factory, creating it on first call."""
    global _session_factory  # noqa: PLW0603

    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )

    return _session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async database session.

    Usage::

        @router.get("/items")
        async def list_items(db: AsyncSession = Depends(get_db)):
            ...
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Create all ORM tables that don't already exist.

    Must be called once at application startup (see ``main.py`` lifespan).
    """
    # Import here to avoid circular imports – models register on Base.metadata
    from polyagent.models import Base  # noqa: F811

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created / verified.")


async def close_db() -> None:
    """Dispose of the engine and release the connection pool."""
    global _engine, _session_factory  # noqa: PLW0603

    if _engine is not None:
        await _engine.dispose()
        logger.info("Database engine disposed.")
        _engine = None
        _session_factory = None
