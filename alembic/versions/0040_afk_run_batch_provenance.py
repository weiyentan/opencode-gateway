"""Add batch provenance for the provisional AFK run lifecycle (issue #595).

The provisional lifecycle (migration 0039) provisions one ``afk_run_id``
for an accepted webhook batch **before** AWX launches.  This migration adds
the batch-provenance contract:

* **``afk_runs.first_delivery_id``** — the first triggering delivery of the
  accepted batch, stored on the run row itself.  Nullable and additive:
  legacy rows (backfill/reconstruction, migration 0026) and runs provisioned
  without a batch read back as ``NULL``.
* **``afk_run_delivery_batches``** — one row per contributing delivery
  identity of the accepted batch, preserving every identity in batch order
  (``position``).  Keyed by ``UNIQUE (afk_run_id, delivery_id)`` so the same
  batch never duplicates a row; ``ON DELETE CASCADE`` ties batch rows to
  their run so a removed run never leaves orphan provenance.

Write-semantics contract (enforced by the repository):

* **Idempotent batch** — the batch rows are written atomically with the run
  INSERT; replaying the same provisioning payload issues no writes, and a
  conflicting batch (different or omitted deliveries) is rejected without
  mutation — batch provenance is never erased by a replay that omits it.
* **Additive-only** — the single column is nullable and the table is new;
  no existing row or read path is affected.

Downgrade drops the table and the column, restoring the 0039 shape exactly.

Revision ID: 0040
Revises:     0039
Create Date: 2026-08-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0040"
down_revision: Union[str, None] = "0039"  # noqa: UP007
branch_labels: Union[str, Sequence[str], None] = None  # noqa: UP007
depends_on: Union[str, Sequence[str], None] = None  # noqa: UP007


def upgrade() -> None:
    """Add batch provenance: ``first_delivery_id`` + the batch table (additive)."""
    # First triggering delivery on the run row — nullable for legacy rows and
    # runs provisioned without a batch.
    op.add_column(
        "afk_runs",
        sa.Column("first_delivery_id", sa.String(), nullable=True),
    )

    # One row per contributing delivery identity, in accepted-batch order.
    op.create_table(
        "afk_run_delivery_batches",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "afk_run_id",
            sa.String(26),
            sa.ForeignKey("afk_runs.afk_run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("delivery_id", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Idempotent batch membership — the same (run, delivery) never
        # duplicates a row.
        sa.UniqueConstraint(
            "afk_run_id",
            "delivery_id",
            name="uq_afk_run_delivery_batches_run_delivery",
        ),
    )


def downgrade() -> None:
    """Drop the batch table and the run column (restores the 0039 shape)."""
    op.drop_table("afk_run_delivery_batches")
    op.drop_column("afk_runs", "first_delivery_id")
