"""Add commit_sha and failure_reason columns to gateway_jobs

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-19 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE gateway_jobs "
        "ADD COLUMN IF NOT EXISTS commit_sha TEXT"
    )
    op.execute(
        "ALTER TABLE gateway_jobs "
        "ADD COLUMN IF NOT EXISTS failure_reason TEXT"
    )


def downgrade() -> None:
    op.drop_column("gateway_jobs", "failure_reason")
    op.drop_column("gateway_jobs", "commit_sha")
