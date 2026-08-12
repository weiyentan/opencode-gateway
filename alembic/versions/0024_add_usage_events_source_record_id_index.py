"""Add index on usage_events(source_record_id) for batch overlap queries.

The batch-level overlap check (issue #416) queries ``usage_events`` by
``source_record_id = ANY($2::text[])`` with a join to ``source_identities``
and a NOT-IN excluded resolved identities.  Without this index PostgreSQL
must scan the ``usage_events`` table sequentially for every batch.
The new index covers the ``source_record_id`` column directly so
``= ANY()`` lookups against a set of incoming record IDs from a 100-record
batch use index scans instead.

Revision ID: 0024
Revises:     0023
Create Date: 2026-08-12
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create index on usage_events(source_record_id)."""
    op.create_index(
        "ix_usage_events_source_record_id",
        "usage_events",
        ["source_record_id"],
    )


def downgrade() -> None:
    """Drop the source_record_id index."""
    op.drop_index(
        "ix_usage_events_source_record_id",
        table_name="usage_events",
    )
