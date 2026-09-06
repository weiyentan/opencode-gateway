"""Add the AFK-run execution binding lookup index.

Revision ID: 0044
Revises:     0043
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0044"
down_revision: Union[str, None] = "0043"  # noqa: UP007
branch_labels: Union[str, Sequence[str], None] = None  # noqa: UP007
depends_on: Union[str, Sequence[str], None] = None  # noqa: UP007


def upgrade() -> None:
    """Support filtering and deterministic ordering by AFK run."""
    op.create_index(
        "ix_execution_bindings_afk_run_created",
        "execution_bindings",
        ["afk_run_id", "created_at", "id"],
    )


def downgrade() -> None:
    """Remove the AFK-run lookup index."""
    op.drop_index(
        "ix_execution_bindings_afk_run_created",
        table_name="execution_bindings",
    )
