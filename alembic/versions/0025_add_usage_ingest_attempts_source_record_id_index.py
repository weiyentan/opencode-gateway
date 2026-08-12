"""Add the source-record lookup index on usage_ingest_attempts used by
the batch overlap check's complete-delivery-history leg.

Backs the ``usage_ingest_attempts`` leg of ``check_batch_overlap``
(issue #416, PR #418).  The existing composite index
``ix_usage_ingest_attempts_source_identity_source_record`` has
``source_identity_id`` as its leading column and cannot serve a
source-record-first ``= ANY(...)`` scan.

Revision ID: 0025
Revises:     0024
Create Date: 2026-08-12
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the lookup index without blocking production writes."""
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_usage_ingest_attempts_original_source_record_id",
            "usage_ingest_attempts",
            ["original_source_record_id"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    """Drop the lookup index without blocking production writes."""
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_usage_ingest_attempts_original_source_record_id",
            table_name="usage_ingest_attempts",
            postgresql_concurrently=True,
        )
