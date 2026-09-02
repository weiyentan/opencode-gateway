from __future__ import annotations

from collections.abc import Sequence

from alembic import op

"""Add the source-record lookup index used by batch overlap checks.

Revision ID: 0024
Revises:     0023
Create Date: 2026-08-12
"""

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the lookup index without blocking production writes."""
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_usage_events_source_record_id",
            "usage_events",
            ["source_record_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    """Drop the lookup index without blocking production writes."""
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_usage_events_source_record_id",
            table_name="usage_events",
            postgresql_concurrently=True,
        )
