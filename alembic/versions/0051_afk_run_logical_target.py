"""Add the immutable logical execution target to ``afk_runs`` (issue #856).

The logical execution target is the explicitly selected worker target bound
**once** to the canonical ``afk_run_id`` at provisioning time (the existing
``POST /api/v1/afk/executions/runs`` authority).  It is a routing decision,
not an execution result: subsequent bot, reviewer, or developer events can
never change it, and a conflicting provisioning replay is rejected by the
repository conflict rules (no reassignment, no second run ID).

This migration is strictly additive: ``logical_target`` is nullable with no
server default, so every historic ``afk_runs`` row (and every run
provisioned by a client that does not send a target) reads back as
``NULL`` — no historic row is rewritten and no target is fabricated.

Downgrade drops the nullable column, restoring the migration 0050 shape;
historic rows are untouched.

Revision ID: 0051
Revises:     0050
Create Date: 2026-11-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0051"
down_revision: Union[str, None] = "0050"  # noqa: UP007
branch_labels: Union[str, Sequence[str], None] = None  # noqa: UP007
depends_on: Union[str, Sequence[str], None] = None  # noqa: UP007


def upgrade() -> None:
    """Add the nullable logical-target column (additive-only)."""
    op.add_column(
        "afk_runs",
        sa.Column("logical_target", sa.String(length=100), nullable=True),
    )


def downgrade() -> None:
    """Drop the logical-target column (restores the 0050 shape)."""
    op.drop_column("afk_runs", "logical_target")
