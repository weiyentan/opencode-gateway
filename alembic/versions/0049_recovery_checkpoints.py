"""Add the AFK recovery-checkpoint table (issue #754).

Creates one additive table — ``recovery_checkpoints`` — without touching any
existing table.  A recovery checkpoint is durable recovery metadata produced
by a failed AFK develop execution: the emergency branch/ref that survived the
failure plus the commit SHA it points at.  It is NOT a new AFK run and never
rewrites the originating execution's failed outcome — it is execution-scoped
metadata attached to an existing AWX execution.

Write/read contract (enforced by the constraints below):

* **Execution-scoped** — every checkpoint references one existing
  ``execution_bindings`` row by its natural key ``awx_job_id`` and inherits
  that execution's ``afk_run_id``.  The FK to
  ``execution_bindings.awx_job_id`` cascades on delete with the owning
  execution.
* **Idempotent by (execution, ref, commit_sha)** — ``UNIQUE`` on
  ``(awx_job_id, ref, commit_sha)`` makes a repeated POST a no-op.  Distinct
  refs pointing at the same SHA are preserved as distinct rows because the
  uniqueness key includes ``ref``.
* **Failed executions keep their outcome** — writing checkpoints never
  touches the owning execution row.
* **Deterministic list order** — served by the ``awx_job_id`` index (the
  unique constraint's leading column) plus ``ORDER BY created_at, id`` in
  the repository.

Downgrade drops the table and its indexes.  The ``execution_bindings`` and
``afk_runs`` rows are untouched.

Revision ID: 0049
Revises:     0048
Create Date: 2026-10-03
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0049"
down_revision: Union[str, None] = "0048"  # noqa: UP007
branch_labels: Union[str, Sequence[str], None] = None  # noqa: UP007
depends_on: Union[str, Sequence[str], None] = None  # noqa: UP007


def upgrade() -> None:
    """Create the execution-scoped recovery-checkpoint table (additive)."""
    op.create_table(
        "recovery_checkpoints",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # The owning AWX execution — natural key of execution_bindings
        # (UNIQUE there).  Deleting the execution removes its checkpoints.
        sa.Column(
            "awx_job_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "execution_bindings.awx_job_id",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        # The owning AFK run, inherited from the execution binding.  Nullable
        # to stay consistent with legacy execution bindings that predate the
        # mandatory afk_run_id (issue #626); new bindings always carry one.
        sa.Column(
            "afk_run_id",
            sa.String(length=26),
            sa.ForeignKey(
                "afk_runs.afk_run_id",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
        # The emergency recovery branch/ref (unbounded text).
        sa.Column("ref", sa.Text(), nullable=False),
        # The commit SHA the ref points at (unbounded text — SHA-1/SHA-256).
        sa.Column("commit_sha", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Idempotency key: same execution + ref + SHA is one row; distinct
        # refs on one SHA remain distinct rows.
        sa.UniqueConstraint(
            "awx_job_id",
            "ref",
            "commit_sha",
            name="uq_recovery_checkpoints_execution_ref_sha",
        ),
    )
    # Execution-scoped list scans (the unique constraint's leading column
    # already serves these; the explicit index keeps the intent documented).
    op.create_index(
        "ix_recovery_checkpoints_awx_job_id",
        "recovery_checkpoints",
        ["awx_job_id"],
    )
    # Run-scoped lookups across every execution of one AFK run.
    op.create_index(
        "ix_recovery_checkpoints_afk_run_id",
        "recovery_checkpoints",
        ["afk_run_id"],
    )


def downgrade() -> None:
    """Drop the recovery-checkpoint table."""
    op.drop_index(
        "ix_recovery_checkpoints_afk_run_id",
        table_name="recovery_checkpoints",
    )
    op.drop_index(
        "ix_recovery_checkpoints_awx_job_id",
        table_name="recovery_checkpoints",
    )
    op.drop_table("recovery_checkpoints")
