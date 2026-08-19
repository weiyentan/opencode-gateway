"""Add fact identity provenance to engineering_events (issue #523).

Adds three additive columns to ``engineering_events``:

* ``observation_key`` — a deterministic, NOT NULL, UNIQUE natural key for
  each immutable fact, derived from the fact's six identity fields
  ``(provider, repository, entity_type, external_id, event_type,
  occurred_at)`` — the same fields as the existing 6-column identity UNIQUE
  (``uq_engineering_events_identity``, migration 0026).  Webhook facts
  derive it at ingest; existing rows are backfilled below with the same
  derivation (shared ``afk_outcomes.models.build_observation_key``) so a
  later redelivery of a pre-existing fact derives the identical key.
* ``observed_via`` — provenance of the observation (``webhook`` or
  ``backfill``), NOT NULL with a ``webhook`` server default.
* ``snapshot_at`` — observation time, distinct from the provider occurrence
  time (``occurred_at``); nullable (pre-0035 rows have no known observation
  time).

The existing 6-column identity constraint is untouched: ``observation_key``
is additive (a content-stable natural key), never a replacement for the
identity UNIQUE, and the write path (``delivery_log`` +
``engineering_events`` single transaction, ``ON CONFLICT ... DO NOTHING`` on
the 6-column identity) is unchanged.

The backfill derives keys in Python with the shared helper — the single
source of truth for the canonical derivation — so backfilled keys cannot
drift from the consumer's canonical form.

Downgrade drops the three columns and the observation_key UNIQUE
(reversible; only the provenance fields themselves are lost).

Revision ID: 0035
Revises:     0034
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "0035"
down_revision: Union[str, None] = "0034"  # noqa: UP007
branch_labels: Union[str, Sequence[str], None] = None  # noqa: UP007
depends_on: Union[str, Sequence[str], None] = None  # noqa: UP007


def upgrade() -> None:
    # ── additive columns (observation_key nullable first, so the backfill
    #    can populate it before NOT NULL is enforced) ────────────────────
    op.add_column(
        "engineering_events",
        sa.Column("observation_key", sa.String(), nullable=True),
    )
    op.add_column(
        "engineering_events",
        sa.Column(
            "observed_via",
            sa.String(),
            nullable=False,
            server_default=sa.text("'webhook'"),
        ),
    )
    op.add_column(
        "engineering_events",
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=True),
    )

    # ── defensive backfill: deterministic observation_key for existing rows ──
    # The shared helper is the single source of truth for the canonical
    # derivation, so backfilled keys match what the consumer derives for a
    # redelivery of the same fact.  Skipped in offline (SQL-render) mode,
    # where there are no rows to backfill.
    if not context.is_offline_mode():
        from afk_outcomes.models import build_observation_key

        bind = op.get_bind()
        rows = bind.execute(
            sa.text(
                "SELECT id, provider, repository, entity_type, external_id, "
                "event_type, occurred_at FROM engineering_events "
                "WHERE observation_key IS NULL"
            )
        ).fetchall()
        for row in rows:
            key = build_observation_key(
                provider=row.provider,
                repository=row.repository,
                entity_type=row.entity_type,
                external_id=row.external_id,
                event_type=row.event_type,
                occurred_at=row.occurred_at,
            )
            bind.execute(
                sa.text(
                    "UPDATE engineering_events SET observation_key = :key "
                    "WHERE id = :id"
                ),
                {"key": key, "id": row.id},
            )

    # ── NOT NULL + UNIQUE after the backfill ───────────────────────────
    op.alter_column(
        "engineering_events",
        "observation_key",
        existing_type=sa.String(),
        nullable=False,
    )
    op.create_unique_constraint(
        "uq_engineering_events_observation_key",
        "engineering_events",
        ["observation_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_engineering_events_observation_key",
        "engineering_events",
        type_="unique",
    )
    op.drop_column("engineering_events", "snapshot_at")
    op.drop_column("engineering_events", "observed_via")
    op.drop_column("engineering_events", "observation_key")
