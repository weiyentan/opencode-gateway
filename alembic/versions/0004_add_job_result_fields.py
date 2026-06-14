"""Add branch_name, mr_url, and workflow_run_id columns to gateway_jobs

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-14 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE gateway_jobs "
        "ADD COLUMN IF NOT EXISTS branch_name TEXT"
    )
    op.execute(
        "ALTER TABLE gateway_jobs "
        "ADD COLUMN IF NOT EXISTS mr_url TEXT"
    )
    op.execute(
        "ALTER TABLE gateway_jobs "
        "ADD COLUMN IF NOT EXISTS workflow_run_id TEXT"
    )


def downgrade() -> None:
    op.drop_column("gateway_jobs", "workflow_run_id")
    op.drop_column("gateway_jobs", "mr_url")
    op.drop_column("gateway_jobs", "branch_name")
