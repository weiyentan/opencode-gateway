"""Add CHECK constraint on workspaces.port for the 10000–10999 range.

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-19

Per ADR 0003, port values must be in the range 10000–10999.  This
migration adds a CHECK constraint that enforces this at the database
level.  NULL ports (workspaces that haven't been assigned a port yet)
are allowed through the constraint.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_workspaces_port_range",
        "workspaces",
        "port IS NULL OR (port >= 10000 AND port <= 10999)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_workspaces_port_range", "workspaces", type_="check")
