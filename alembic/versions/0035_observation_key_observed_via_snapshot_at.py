"""Add fact identity provenance to engineering_events (issue #523).

Adds three additive columns to ``engineering_events``:

* ``observation_key`` — a deterministic, NOT NULL, UNIQUE natural key for
  each immutable fact, derived from the fact's six identity fields
  ``(provider, repository, entity_type, external_id, event_type,
  occurred_at)`` — the same fields as the existing 6-column identity UNIQUE
  (``uq_engineering_events_identity``, migration 0026).  Webhook facts
  derive it at ingest; existing rows are backfilled below with the same
  derivation so a later redelivery of a pre-existing fact derives the
  identical key.
* ``observed_via`` — provenance of the observation (``webhook`` or
  ``backfill``), NOT NULL with a ``webhook`` server default.  Pre-existing
  rows were all written by the backfill engine, so they are set to
  ``backfill`` below; future inserts omitting the value still default to
  ``webhook``.
* ``snapshot_at`` — observation time, distinct from the provider occurrence
  time (``occurred_at``); nullable (pre-0035 rows have no known observation
  time).

The existing 6-column identity constraint is untouched: ``observation_key``
is additive (a content-stable natural key), never a replacement for the
identity UNIQUE, and the write path (``delivery_log`` +
``engineering_events`` single transaction, ``ON CONFLICT ... DO NOTHING`` on
the 6-column identity) is unchanged.

The backfill derives keys with a self-contained, version-pinned copy of the
canonical derivation (:func:`_observation_key`) — migrations must never
import live application code, whose semantics can drift after a migration is
authored.  The copy produces byte-identical keys to
``afk_outcomes.models.build_observation_key`` at authoring time (pinned by
``tests/test_migration_0035.py``), and is applied as a single batched UPDATE
rather than row-by-row.

Downgrade drops the three columns and the observation_key UNIQUE
(reversible; only the provenance fields themselves are lost).

Revision ID: 0035
Revises:     0034
"""

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime, timezone
from enum import Enum
from typing import Union

import sqlalchemy as sa

from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "0035"
down_revision: Union[str, None] = "0034"  # noqa: UP007
branch_labels: Union[str, Sequence[str], None] = None  # noqa: UP007
depends_on: Union[str, Sequence[str], None] = None  # noqa: UP007


def _observation_key(
    *,
    provider: object,
    repository: str,
    entity_type: object,
    external_id: str,
    event_type: str,
    occurred_at: datetime,
) -> str:
    """Frozen snapshot of ``afk_outcomes.models.build_observation_key``.

    Self-contained, version-pinned copy of the canonical derivation at
    migration authoring time: SHA-256 over the canonical JSON form of the
    fact's six identity fields.  Migrations must not import live application
    code (semantics drift after authoring), so this stdlib-only copy keeps
    the backfilled keys identical to what the consumer derives for a
    redelivery of the same fact.  ``provider``/``entity_type`` are
    ``object``-typed because database rows arrive as plain strings while the
    canonical helper accepts ``str`` Enums; enum members are reduced to
    their values, and a naive ``occurred_at`` is interpreted as UTC.
    """
    provider_value = (
        provider.value if isinstance(provider, Enum) else str(provider)
    )
    entity_type_value = (
        entity_type.value if isinstance(entity_type, Enum) else str(entity_type)
    )
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    occurred_at_value = occurred_at.astimezone(timezone.utc).isoformat()
    canonical = json.dumps(
        {
            "provider": provider_value,
            "repository": repository,
            "entity_type": entity_type_value,
            "external_id": external_id,
            "event_type": event_type,
            "occurred_at": occurred_at_value,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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

    # ── backfill: deterministic observation_key + observed_via for
    #    pre-existing rows (skipped in offline/SQL-render mode, where there
    #    are no rows to backfill) ────────────────────────────────────────
    if not context.is_offline_mode():
        bind = op.get_bind()
        rows = bind.execute(
            sa.text(
                "SELECT id, provider, repository, entity_type, external_id, "
                "event_type, occurred_at FROM engineering_events "
                "WHERE observation_key IS NULL"
            )
        ).fetchall()
        params = [
            {
                "key": _observation_key(
                    provider=r.provider,
                    repository=r.repository,
                    entity_type=r.entity_type,
                    external_id=r.external_id,
                    event_type=r.event_type,
                    occurred_at=r.occurred_at,
                ),
                "id": r.id,
            }
            for r in rows
        ]
        if params:
            bind.execute(
                sa.text("UPDATE engineering_events SET observation_key = :key WHERE id = :id"),
                params,
            )
        # Every pre-existing row was written by the backfill engine before
        # provenance existed — it must never masquerade as a webhook fact.
        bind.execute(sa.text("UPDATE engineering_events SET observed_via = 'backfill'"))

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
