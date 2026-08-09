"""Make opencode_usage_records.session_id nullable.

The atomic dedup INSERT ... ON CONFLICT ... DO NOTHING writes the usage
record with a NULL session_id and backfills it on the winner path within
the same explicit transaction (see F2), so the column must be nullable.

Revision ID: 0020
Revises:     0019
Create Date: 2026-08-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Make session_id nullable so atomic dedup INSERT can use NULL."""
    op.alter_column(
        "opencode_usage_records",
        "session_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )


def downgrade() -> None:
    """Restore session_id NOT NULL (reverting the atomic dedup pattern)."""
    op.alter_column(
        "opencode_usage_records",
        "session_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
