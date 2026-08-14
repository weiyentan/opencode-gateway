"""Add retention indexes on the transcript tables' ``source_created_at_tz`` (issue #470).

The execution-transcript retention job (``scripts/retention_transcripts.py``)
deletes rows strictly older than a per-table cutoff keyed on
``source_created_at_tz`` (``WHERE source_created_at_tz < $1``).  Migration 0029
indexed ``source_created_at`` (BigInt) and various composite columns, but never
``source_created_at_tz`` — so every retention batch is a full table scan.

0030 adds one partial btree index per transcript table on
``source_created_at_tz``.  The index is partial (``WHERE
source_created_at_tz IS NOT NULL``) because rows with a NULL source timestamp
are never eligible for retention (their age is unknown and they are retained
forever), so they are never scanned by the retention predicate; the partial
index is smaller and cheaper to maintain than a full-column index.

Revision ID: 0030
Revises:     0029
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the partial retention indexes on ``source_created_at_tz``."""
    op.create_index(
        "ix_observed_messages_retention",
        "observed_messages",
        ["source_created_at_tz"],
        postgresql_where=sa.text("source_created_at_tz IS NOT NULL"),
    )
    op.create_index(
        "ix_observed_parts_retention",
        "observed_parts",
        ["source_created_at_tz"],
        postgresql_where=sa.text("source_created_at_tz IS NOT NULL"),
    )
    op.create_index(
        "ix_observed_tool_calls_retention",
        "observed_tool_calls",
        ["source_created_at_tz"],
        postgresql_where=sa.text("source_created_at_tz IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop the retention indexes in reverse creation order."""
    op.drop_index(
        "ix_observed_tool_calls_retention",
        table_name="observed_tool_calls",
        postgresql_where=sa.text("source_created_at_tz IS NOT NULL"),
    )
    op.drop_index(
        "ix_observed_parts_retention",
        table_name="observed_parts",
        postgresql_where=sa.text("source_created_at_tz IS NOT NULL"),
    )
    op.drop_index(
        "ix_observed_messages_retention",
        table_name="observed_messages",
        postgresql_where=sa.text("source_created_at_tz IS NOT NULL"),
    )
