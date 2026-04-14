"""SQLAlchemy async engine and session."""

from collections.abc import AsyncGenerator
from typing import Annotated

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from fastapi import Depends

from app.config import get_settings

_settings = get_settings()

# PgBouncer (e.g. Supabase pooler :6543, transaction mode) reuses backend sessions; asyncpg’s
# prepared-statement cache then raises DuplicatePreparedStatementError. Disable statement cache.
engine = create_async_engine(
    _settings.database_url,
    echo=_settings.debug,
    pool_pre_ping=True,
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
