"""Add afk_run_id and trigger_type to execution_bindings (issue #582).

Adds two nullable columns to the existing ``execution_bindings`` table:

* ``afk_run_id`` — VARCHAR(26) FK to ``afk_runs.afk_run_id``,
  ON DELETE SET NULL.  Links a binding to the AFK run it produced
  (populated by a later slice).
* ``trigger_type`` — nullable VARCHAR carrying the trigger origin
  (populated by a later slice).

Both columns are nullable and additive — no existing row is affected.

Downgrade drops both columns.

Revision ID: 0038
Revises:     0037
Create Date: 2026-08-23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0038"
down_revision: Union[str, None] = "0037"  # noqa: UP007
branch_labels: Union[str, Sequence[str], None] = None  # noqa: UP007
depends_on: Union[str, Sequence[str], None] = None  # noqa: UP007


def upgrade() -> None:
    """Add afk_run_id (FK) and trigger_type columns to execution_bindings."""
    op.add_column(
        "execution_bindings",
        sa.Column(
            "afk_run_id",
            sa.String(26),
            sa.ForeignKey("afk_runs.afk_run_id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "execution_bindings",
        sa.Column(
            "trigger_type",
            sa.String(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Drop the afk_run_id and trigger_type columns."""
    op.drop_column("execution_bindings", "trigger_type")
    op.drop_column("execution_bindings", "afk_run_id")
