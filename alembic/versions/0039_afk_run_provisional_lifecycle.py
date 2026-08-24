"""Add the provisional AFK run lifecycle columns to ``afk_runs`` (issue #589).

The provisional lifecycle is the Gateway-owned anchor for an AFK run:
provisioned **before** the AWX launch with source provenance, a
provider-qualified repository identity, and trigger metadata — with
nullable AWX/change-request fields populated later.  This migration is
additive-only: every column is nullable, so existing ``afk_runs`` rows
(backfill/reconstruction, migration 0026) remain valid untouched.

Write-semantics contract (enforced by the repository, guaranteed by the
constraints below):

* **Idempotent provisioning** — keyed by the partial unique index
  ``uq_afk_runs_provisioning_key`` over
  ``(provider, host, source_event_id) WHERE host IS NOT NULL AND
  source_event_id IS NOT NULL``.  Replaying the same provisioning payload
  never creates a duplicate row; legacy rows (NULL ``host`` /
  ``source_event_id``) are excluded from the key entirely.
* **1:1 lifecycle<->change_request** — each lifecycle owns at most one
  change request (scalar columns), and each change request belongs to at
  most one lifecycle, enforced by the partial unique index
  ``uq_afk_runs_change_request_identity`` over
  ``(change_request_provider, change_request_repository,
  change_request_external_id)`` where all three are NOT NULL.  Unbound
  rows (any of the three NULL) are excluded.
* **Recovery without predecessor mutation** — ``recovered_from_afk_run_id``
  is a nullable self-referential FK (``ON DELETE SET NULL``) that lets a
  recovery lifecycle reference its predecessor; creating a recovery
  lifecycle never updates the predecessor row.

Downgrade drops the two partial unique indexes, the lookup index, and all
nine columns — restoring the 0038 shape exactly.

Revision ID: 0039
Revises:     0038
Create Date: 2026-08-24
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0039"
down_revision: Union[str, None] = "0038"  # noqa: UP007
branch_labels: Union[str, Sequence[str], None] = None  # noqa: UP007
depends_on: Union[str, Sequence[str], None] = None  # noqa: UP007


def upgrade() -> None:
    """Add the provisional lifecycle columns, indexes, and constraints (additive)."""
    # Source provenance + idempotency key parts.
    op.add_column("afk_runs", sa.Column("host", sa.String(), nullable=True))
    op.add_column(
        "afk_runs", sa.Column("source_event_id", sa.String(), nullable=True)
    )
    # Provider-qualified repository identity + trigger metadata.
    op.add_column("afk_runs", sa.Column("repository", sa.String(), nullable=True))
    op.add_column("afk_runs", sa.Column("trigger_type", sa.String(), nullable=True))
    # Change-request binding — nullable until bound; the three columns
    # together form the flattened stable resource identity.
    op.add_column(
        "afk_runs",
        sa.Column("change_request_provider", sa.String(), nullable=True),
    )
    op.add_column(
        "afk_runs",
        sa.Column("change_request_repository", sa.String(), nullable=True),
    )
    op.add_column(
        "afk_runs",
        sa.Column("change_request_external_id", sa.String(), nullable=True),
    )
    # Recovery reference — a recovery lifecycle references its predecessor
    # without mutating it; SET NULL keeps the recovery row if the
    # predecessor is ever removed.
    op.add_column(
        "afk_runs",
        sa.Column(
            "recovered_from_afk_run_id",
            sa.String(26),
            sa.ForeignKey("afk_runs.afk_run_id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # Idempotency key: provider + host + source_event_id.  Partial — legacy
    # rows (NULL host/source_event_id) never participate.
    op.create_index(
        "uq_afk_runs_provisioning_key",
        "afk_runs",
        ["provider", "host", "source_event_id"],
        unique=True,
        postgresql_where=sa.text(
            "host IS NOT NULL AND source_event_id IS NOT NULL"
        ),
    )

    # 1:1 lifecycle<->change_request: a change request belongs to at most
    # one lifecycle.  Partial — unbound rows (any of the three NULL) never
    # participate.
    op.create_index(
        "uq_afk_runs_change_request_identity",
        "afk_runs",
        [
            "change_request_provider",
            "change_request_repository",
            "change_request_external_id",
        ],
        unique=True,
        postgresql_where=sa.text(
            "change_request_provider IS NOT NULL AND "
            "change_request_repository IS NOT NULL AND "
            "change_request_external_id IS NOT NULL"
        ),
    )

    # Lookup index for recovery relationships (find descendants by predecessor).
    op.create_index(
        "ix_afk_runs_recovered_from",
        "afk_runs",
        ["recovered_from_afk_run_id"],
    )


def downgrade() -> None:
    """Drop the lifecycle indexes and columns (restores the 0038 shape)."""
    op.drop_index("ix_afk_runs_recovered_from", table_name="afk_runs")
    op.drop_index("uq_afk_runs_change_request_identity", table_name="afk_runs")
    op.drop_index("uq_afk_runs_provisioning_key", table_name="afk_runs")

    op.drop_column("afk_runs", "recovered_from_afk_run_id")
    op.drop_column("afk_runs", "change_request_external_id")
    op.drop_column("afk_runs", "change_request_repository")
    op.drop_column("afk_runs", "change_request_provider")
    op.drop_column("afk_runs", "trigger_type")
    op.drop_column("afk_runs", "repository")
    op.drop_column("afk_runs", "source_event_id")
    op.drop_column("afk_runs", "host")
