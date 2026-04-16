"""SQLAlchemy async engine and session."""

from collections.abc import AsyncGenerator
from typing import Annotated

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from fastapi import Depends

from app.config import get_settings

_settings = get_settings()

# asyncpg: disable server-side prepared statement cache when using PgBouncer poolers
# (avoids DuplicatePreparedStatementError with transaction mode).
# Pool size: keep modest when sharing Supabase Session pooler with other services (e.g. Comms).
engine = create_async_engine(
    _settings.database_url,
    echo=_settings.debug,
    pool_pre_ping=True,
    pool_size=_settings.db_pool_size,
    max_overflow=_settings.db_max_overflow,
    pool_timeout=_settings.db_pool_timeout,
    connect_args={"statement_cache_size": 0},
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session


DbSession = Annotated[AsyncSession, Depends(get_db)]
