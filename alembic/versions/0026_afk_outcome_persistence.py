"""Add the AFK outcome persistence schema (issue #448).

Creates six additive tables — ``afk_runs``, ``afk_run_sessions``,
``afk_run_entities``, ``engineering_events``, ``delivery_log``, and
``unresolved_correlations`` — without touching any existing usage table
(``usage_events``, ``opencode_usage_records``, ``sessions``,
``source_identities``, ``client_project_rollup``, etc.).

These tables back the AFK Outcome Observability read-model.  The domain
package ``afk_outcomes`` supplies the neutral models; this migration is
their persistent form, and the ``afk_outcomes.repository``
``AsyncpgOutcomeRepository`` is the only writer.

Write-semantics contract (enforced by the repository, guaranteed by the
constraints below):

* **Event identity** — ``engineering_events`` is keyed by
  ``UNIQUE (provider, repository, entity_type, external_id, event_type,
  occurred_at)``.  Engineering events are immutable facts: a re-delivery of
  the same event no-ops via ``ON CONFLICT DO NOTHING``.  ``provider_event_id``
  is stored (nullable) and, where a provider emits one, is the authority for
  ``occurred_at`` (the provider's own timestamp is already carried on the
  domain event's ``occurred_at``).
* **Entity mapping** — ``afk_run_entities`` is keyed by
  ``UNIQUE (provider, repository, entity_type, external_id, afk_run_id)``;
  ``afk_run_id`` is ``NOT NULL``.  One row per (entity, run) association.
* **Delivery log** — ``delivery_log`` is keyed by
  ``UNIQUE (provider, delivery_id)`` and written with
  ``ON CONFLICT DO NOTHING`` so a delivery is processed at most once.
* **Link rows** keep ``afk_run_id NOT NULL`` (both ``afk_run_sessions`` and
  ``afk_run_entities``).
* **Enrich-only** — state rows are never hard-deleted and never have their
  confidence silently lowered; superseded entity links are marked via
  ``superseded_at`` rather than removed; unresolved correlations are written
  to ``unresolved_correlations`` only.

Foreign keys toward ``afk_runs`` use ``ondelete="CASCADE"`` (children are
cleaned up with their run), mirroring the 0021 convention for FKs within a
single migration's new tables.  ``delivery_log.afk_run_id`` is a nullable
audit reference and carries no ``ON DELETE`` action.  ``afk_run_sessions``
and ``unresolved_correlations`` reference the OpenCode session identity and
run id loosely (nullable, no FK) so they never couple this schema to the
exact shape of the ``sessions`` table.

Revision ID: 0026
Revises:     0025
Create Date: 2026-08-14
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the six AFK outcome persistence tables (additive)."""

    # ── afk_runs ─────────────────────────────────────────────────────
    op.create_table(
        "afk_runs",
        sa.Column("afk_run_id", sa.String(26), primary_key=True),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome_status", sa.String(), nullable=True),
        sa.Column("outcome", postgresql.JSONB(), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ── afk_run_sessions ─────────────────────────────────────────────
    op.create_table(
        "afk_run_sessions",
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
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("external_session_id", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "afk_run_id",
            "external_session_id",
            name="uq_afk_run_sessions_run_external_session",
        ),
    )
    op.create_index(
        "ix_afk_run_sessions_afk_run_id",
        "afk_run_sessions",
        ["afk_run_id"],
    )

    # ── afk_run_entities ─────────────────────────────────────────────
    op.create_table(
        "afk_run_entities",
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
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("repository", sa.String(), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("correlation_method", sa.String(), nullable=True),
        sa.Column("correlation_confidence", sa.Float(), nullable=False),
        sa.Column(
            "evidence",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("resolver_version", sa.String(), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "provider",
            "repository",
            "entity_type",
            "external_id",
            "afk_run_id",
            name="uq_afk_run_entities_entity_run",
        ),
    )
    op.create_index(
        "ix_afk_run_entities_afk_run_id",
        "afk_run_entities",
        ["afk_run_id"],
    )

    # ── engineering_events ───────────────────────────────────────────
    op.create_table(
        "engineering_events",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("repository", sa.String(), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_event_id", sa.String(), nullable=True),
        sa.Column("actor", sa.String(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "first_ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "provider",
            "repository",
            "entity_type",
            "external_id",
            "event_type",
            "occurred_at",
            name="uq_engineering_events_identity",
        ),
    )
    op.create_index(
        "ix_engineering_events_entity",
        "engineering_events",
        ["provider", "repository", "entity_type", "external_id"],
    )

    # ── delivery_log ─────────────────────────────────────────────────
    op.create_table(
        "delivery_log",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("delivery_id", sa.String(), nullable=False),
        sa.Column(
            "afk_run_id",
            sa.String(26),
            sa.ForeignKey("afk_runs.afk_run_id"),
            nullable=True,
        ),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column(
            "delivered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "provider",
            "delivery_id",
            name="uq_delivery_log_provider_delivery",
        ),
    )

    # ── unresolved_correlations ──────────────────────────────────────
    op.create_table(
        "unresolved_correlations",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("repository", sa.String(), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=False),
        sa.Column("afk_run_id", sa.String(26), nullable=True),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("correlation_confidence", sa.Float(), nullable=False),
        sa.Column(
            "evidence",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("resolver_version", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "provider",
            "repository",
            "entity_type",
            "external_id",
            "method",
            name="uq_unresolved_correlations_entity_method",
        ),
    )
    op.create_index(
        "ix_unresolved_correlations_entity",
        "unresolved_correlations",
        ["provider", "repository", "entity_type", "external_id"],
    )


def downgrade() -> None:
    """Drop the six AFK outcome persistence tables in reverse dependency order."""
    op.drop_table("unresolved_correlations")
    op.drop_table("delivery_log")
    op.drop_table("engineering_events")
    op.drop_table("afk_run_entities")
    op.drop_table("afk_run_sessions")
    op.drop_table("afk_runs")
