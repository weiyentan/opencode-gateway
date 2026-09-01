"""Explicit execution→session attribution collection (issue #627).

Adds an additive JSONB column ``external_session_ids`` to
``execution_bindings`` carrying the normalized, deduplicated collection of
external OpenCode session ids attributed to the execution — the first
entry is the primary session and mirrors the existing nullable
``external_session_id`` column.

The singular column is retained for backward compatibility and remains the
authoritative primary-session readback for legacy rows.  The collection
column is nullable: historical / run-level-only bindings with no resolved
session carry NULL (an empty collection), never fabricated ownership.

Downgrade drops the additive column; the singular column is untouched.

Revision ID: 0042
Revises:     0041
Create Date: 2026-09-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0042"
down_revision: Union[str, None] = "0041"  # noqa: UP007
branch_labels: Union[str, Sequence[str], None] = None  # noqa: UP007
depends_on: Union[str, Sequence[str], None] = None  # noqa: UP007


def upgrade() -> None:
    """Add the additive JSONB session-attribution column (nullable)."""
    op.add_column(
        "execution_bindings",
        sa.Column("external_session_ids", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    """Drop the additive JSONB column (singular column untouched)."""
    op.drop_column("execution_bindings", "external_session_ids")
