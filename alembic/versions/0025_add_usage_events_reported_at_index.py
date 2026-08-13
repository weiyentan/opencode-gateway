"""Add the source-created-time ordering index for the records view.

The Records view orders by ``COALESCE(source_created_at_tz, reported_at)``
(source-created message time, issue #401), so the default sort path needs a
``reported_at`` index on ``usage_events`` — the composite
``ix_usage_events_session_reported_at`` (0021) does not cover full-table
ordering scans.

Revision ID: 0025
Revises:     0024
Create Date: 2026-08-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ordering index without blocking production writes."""
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_usage_events_reported_at",
            "usage_events",
            ["reported_at"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    """Drop the ordering index without blocking production writes."""
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_usage_events_reported_at",
            table_name="usage_events",
            postgresql_concurrently=True,
        )
