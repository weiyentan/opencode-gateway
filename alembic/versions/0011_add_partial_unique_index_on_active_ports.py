"""Add partial unique index on workspaces.port for active (non-cleaned) workspaces.

Prevents duplicate port assignments for active workspaces while allowing:
  - NULL ports (workspaces that haven't been assigned a port yet)
  - Duplicate ports on cleaned workspaces (so cleaned records don't block reuse)

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-19

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_workspaces_active_port",
        "workspaces",
        ["port"],
        unique=True,
        postgresql_where=(
            "port IS NOT NULL AND cleanup_status != 'cleaned'"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_workspaces_active_port", table_name="workspaces")
