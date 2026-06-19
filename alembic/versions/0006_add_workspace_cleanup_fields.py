"""Add workspace cleanup timestamp and failure reason columns.

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-19 00:00:00.000000

Adds four nullable columns to the workspaces table to support the
explicit cleanup terminal-state machine (issue #111):

  * cleanup_started_at    — when the cleanup process began
  * cleanup_completed_at  — when cleanup finished successfully (status ``cleaned``)
  * cleanup_failed_at     — when cleanup failed (status ``cleanup_failed``)
  * cleanup_failure_reason — human-readable description of why cleanup failed
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("cleanup_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("cleanup_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("cleanup_failed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "workspaces",
        sa.Column("cleanup_failure_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "cleanup_failure_reason")
    op.drop_column("workspaces", "cleanup_failed_at")
    op.drop_column("workspaces", "cleanup_completed_at")
    op.drop_column("workspaces", "cleanup_started_at")
