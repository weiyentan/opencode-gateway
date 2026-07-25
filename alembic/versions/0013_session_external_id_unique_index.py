"""Add unique partial index on sessions (source_database_id, external_session_id).

Creates ``uq_sessions_external_session_id`` as a partial unique index
that only applies when ``external_session_id IS NOT NULL``, enforcing
the session identity resolution invariant: each external session ID
maps to exactly one internal UUID per source database.

Revision ID: 0013
Revises:     0012
Create Date: 2025-07-25
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add unique partial index for external session ID resolution."""
    op.execute(
        """CREATE UNIQUE INDEX uq_sessions_external_session_id
           ON sessions (source_database_id, external_session_id)
           WHERE external_session_id IS NOT NULL"""
    )


def downgrade() -> None:
    """Remove the unique partial index."""
    op.execute("DROP INDEX IF EXISTS uq_sessions_external_session_id")
