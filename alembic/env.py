"""Alembic environment — sync engine for migrations (app uses async at runtime)."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool, text

from app.config import get_settings
from app.models.base import Base
from app.models import contact, conversation, location, message, organization, prompt  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_sync_url() -> str:
    """Alembic uses a sync engine; map async and bare URLs to psycopg3 (psycopg package)."""
    settings = get_settings()
    url = settings.database_url.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    # Supabase dashboard often copies postgresql://… — without a +driver SQLAlchemy may pick psycopg2
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def run_migrations_offline() -> None:
    url = get_sync_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        version_table_schema="sarah",
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        version_table_schema="sarah",
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(get_sync_url(), poolclass=pool.NullPool)

    with connectable.connect() as connection:
        # Version table lives in sarah.* — schema must exist before Alembic creates it
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS sarah"))
        connection.commit()
        do_run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
