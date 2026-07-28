"""Add total_cache_read_tokens and total_cache_write_tokens to sessions.

These columns store the per-category cache token totals at the session
level, allowing the Gateway to report Cache Activity (read vs write)
separately from the backward-compatible combined total_cached_tokens.

Revision ID: 0016
Revises:     0015
Create Date: 2026-07-28
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add total_cache_read_tokens and total_cache_write_tokens to sessions."""
    op.add_column(
        "sessions",
        sa.Column(
            "total_cache_read_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "sessions",
        sa.Column(
            "total_cache_write_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    """Drop the two new cache token columns from sessions."""
    op.drop_column("sessions", "total_cache_write_tokens")
    op.drop_column("sessions", "total_cache_read_tokens")
