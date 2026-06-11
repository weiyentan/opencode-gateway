"""SQLAlchemy declarative base — coexists with existing asyncpg session management."""

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase

# Use a distinct naming convention so these tables coexist peacefully
# with tables managed via app/db/schema.sql.
convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(AsyncAttrs, DeclarativeBase):
    """Async-aware declarative base for new SQLAlchemy models.

    This base is *separate* from the existing asyncpg session machinery.
    Tables defined here are managed via Alembic migrations, not schema.sql.
    """

    metadata = MetaData(naming_convention=convention)
