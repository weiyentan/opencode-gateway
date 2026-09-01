"""Add an index on ``usage_events.reported_at``.

The Records view always filters by a date range on ``reported_at``
(``our.reported_at >= $1 AND our.reported_at <= $2``), and the explicit
``sort_by=reported_at`` opt-in orders by ``usage_events.reported_at`` alone.
A bare ``reported_at`` index can serve both of those single-table paths.
The composite ``ix_usage_events_session_reported_at`` (0021) leads with
``session_id``, so it cannot serve a whole-table ``reported_at`` range scan
or sort.

This index does **not** cover the Records *default* sort, which is
``source_created_at`` → ``COALESCE(osc.source_created_at_tz,
our.reported_at)`` (issue #401).  That ordering key reads a column of the
*joined* ``opencode_session_contexts`` table, so no ``usage_events``-only
index can satisfy it; the default sort is therefore intentionally
unindexed (a full sort over the filtered result set).  Likewise, the
``sort_by=ingested_at`` opt-in orders by ``first_ingested_at``, not
``reported_at``, and is not covered here either.

Revision ID: 0025
Revises:     0024
Create Date: 2026-08-13
"""

from __future__ import annotations

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
