"""Execution outcome correction audit table (issue #654).

Adds the ``execution_outcome_corrections`` audit table backing the
least-privilege correction path for execution bindings wrongly recorded
as ``cancelled`` (the prose-substring heuristic in the AWX playbooks
matched "cancell" in a coordinator summary — PR #653 / AWX job 9293).

One row per correction, written in the same transaction as the outcome
flip by ``scripts/correct_execution_outcome.py``.  Records the full
before/after story: the corrected binding (FK), its AWX job id, the
previous and new outcome, the previous bounded failure metadata (cleared
on correction because a completed execution carries no Failure Summary),
the operator reason, and the correction time.  Rows are never updated or
deleted — the audit trail is append-only.

Downgrade drops the table; the ``execution_bindings`` rows are untouched
(the script is the only writer of both).

Revision ID: 0043
Revises:     0042
Create Date: 2026-09-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0043"
down_revision: Union[str, None] = "0042"  # noqa: UP007
branch_labels: Union[str, Sequence[str], None] = None  # noqa: UP007
depends_on: Union[str, Sequence[str], None] = None  # noqa: UP007


def upgrade() -> None:
    """Create the append-only execution-outcome correction audit table."""
    op.create_table(
        "execution_outcome_corrections",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "execution_binding_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "execution_bindings.id",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
        sa.Column("awx_job_id", sa.BigInteger(), nullable=False),
        sa.Column("previous_outcome", sa.String(), nullable=False),
        sa.Column("new_outcome", sa.String(), nullable=False),
        sa.Column("previous_failure_reason", sa.String(), nullable=True),
        sa.Column("previous_failure_summary", sa.String(), nullable=True),
        sa.Column("reason", sa.String(length=1000), nullable=False),
        sa.Column(
            "corrected_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_execution_outcome_corrections_awx_job_id",
        "execution_outcome_corrections",
        ["awx_job_id"],
    )
    op.create_index(
        "ix_execution_outcome_corrections_corrected_at",
        "execution_outcome_corrections",
        ["corrected_at"],
    )


def downgrade() -> None:
    """Drop the audit table (append-only — nothing else to unwind)."""
    op.drop_index(
        "ix_execution_outcome_corrections_corrected_at",
        table_name="execution_outcome_corrections",
    )
    op.drop_index(
        "ix_execution_outcome_corrections_awx_job_id",
        table_name="execution_outcome_corrections",
    )
    op.drop_table("execution_outcome_corrections")
