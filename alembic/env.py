"""Alembic async environment — uses app.db.models.Base metadata for autogenerate."""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.core.config import get_settings
from app.db.models import Base  # noqa: F401 — registers all ORM models

# Alembic Config object
config = context.config

# Set up Python logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for autogenerate
target_metadata = Base.metadata


def _get_sync_url() -> str:
    """Build a sync database URL from Gateway settings.

    Used for *offline* mode only.  Alembic itself connects via the
    async URL built in ``run_migrations_online``.
    """
    s = get_settings()
    return (
        f"postgresql+psycopg://{s.database_user}:{s.database_password}"
        f"@{s.database_host}:{s.database_port}/{s.database_name}"
    )


def _get_async_url() -> str:
    """Build an async database URL from Gateway settings."""
    s = get_settings()
    return (
        f"postgresql+asyncpg://{s.database_user}:{s.database_password}"
        f"@{s.database_host}:{s.database_port}/{s.database_name}"
    )


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Configures the context with just a URL and not an Engine.
    Calls to ``context.execute()`` emit the given SQL string to the
    script output.
    """
    url = config.get_main_option("sqlalchemy.url", _get_sync_url())
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations with an async engine."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _get_async_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (async)."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
