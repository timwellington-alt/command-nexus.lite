"""
Database engine and session management.

Provides async engine, session factory, and base model class.
All modules share these conventions.
"""

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=False,
    pool_pre_ping=True,
    # Per-worker pool. With 4 uvicorn workers + worker/scheduler/voice
    # containers each holding their own pool, the total ceiling has to
    # fit under Postgres `max_connections` (currently 300 — see compose).
    # 5 baseline + 10 overflow = 15 per worker × 4 = 60 max from API,
    # leaving plenty of headroom for background containers + ad-hoc
    # admin queries.
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
    pool_recycle=1800,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""
    pass


async def get_db():
    """FastAPI dependency — yields an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def init_db():
    """Initialize database connection pool on startup.

    Schema is managed exclusively by Alembic migrations.
    Run `alembic upgrade head` before starting the app.
    """
    # Verify connectivity
    async with engine.begin() as conn:
        await conn.execute(sa.text("SELECT 1"))
