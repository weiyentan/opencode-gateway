"""Add canonical event schema for replay-safe usage accounting.

Creates: usage_events, usage_ingest_attempts, source_identities,
source_identity_quarantine, source_identity_resolutions.

These tables back the replay-safe usage accounting model (issue #383):
``usage_events`` stores one canonical accounting event per
(identity, source_record_id), ``usage_ingest_attempts`` records every
delivery of a record, and the ``source_identity*`` tables map collector
source identities to clients and quarantine overlapping identities until
they are resolved.  The existing ``opencode_usage_records`` table and all
other pre-existing objects are untouched.

Foreign keys referencing the new ``source_identities`` mapping table use
``ondelete="CASCADE"`` (children are cleaned up with their identity,
mirroring the 0015 convention for the projection tables' FKs toward
``source_databases``); FKs toward pre-existing tables (``opencode_clients``,
``sessions``, ``observed_models``, ``ingest_batches``) follow the repo
default of no ``ON DELETE`` action.

``source_identity_quarantine.resolution_id`` and
``source_identity_resolutions.quarantine_id`` form a circular FK pair, so
both tables are created first and the two FKs are attached afterwards with
``op.create_foreign_key``.

Revision ID: 0021
Revises:     0020
Create Date: 2026-08-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the five canonical event schema tables."""

    # ── source_identities ────────────────────────────────────────────
    op.create_table(
        "source_identities",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "client_id",
            sa.Uuid(),
            sa.ForeignKey("opencode_clients.id"),
            nullable=False,
        ),
        sa.Column("collector_source_id", sa.String(), nullable=False),
        sa.Column(
            "is_canonical",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "canonical_parent_id",
            sa.Uuid(),
            sa.ForeignKey("source_identities.id"),
            nullable=True,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "client_id",
            "collector_source_id",
            name="uq_source_identities_client_source_key",
        ),
    )

    # ── usage_events ─────────────────────────────────────────────────
    op.create_table(
        "usage_events",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "canonical_source_identity_id",
            sa.Uuid(),
            sa.ForeignKey("source_identities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_record_id", sa.String(), nullable=False),
        sa.Column(
            "client_id",
            sa.Uuid(),
            sa.ForeignKey("opencode_clients.id"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("sessions.id"),
            nullable=False,
        ),
        sa.Column(
            "model_id",
            sa.Uuid(),
            sa.ForeignKey("observed_models.id"),
            nullable=False,
        ),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_tokens", sa.Integer(), nullable=False),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_write_tokens", sa.Integer(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Numeric(), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(), nullable=True),
        sa.Column("mode", sa.String(), nullable=True),
        sa.Column("finish_reason", sa.String(), nullable=True),
        sa.Column("project_id", sa.String(), nullable=True),
        sa.Column("workspace_id", sa.String(), nullable=True),
        sa.Column("agent", sa.String(), nullable=True),
        sa.Column("parent_session_id", sa.String(), nullable=True),
        sa.Column(
            "first_ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "canonical_source_identity_id",
            "source_record_id",
            name="uq_usage_events_canonical_source_key",
        ),
    )
    op.create_index(
        "ix_usage_events_session_reported_at",
        "usage_events",
        ["session_id", "reported_at"],
    )
    op.create_index(
        "ix_usage_events_session_model_id",
        "usage_events",
        ["session_id", "model_id"],
    )

    # ── usage_ingest_attempts ────────────────────────────────────────
    op.create_table(
        "usage_ingest_attempts",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "usage_event_id",
            sa.Uuid(),
            sa.ForeignKey("usage_events.id"),
            nullable=True,
        ),
        sa.Column(
            "source_identity_id",
            sa.Uuid(),
            sa.ForeignKey("source_identities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("original_source_record_id", sa.String(), nullable=False),
        sa.Column("record_jsonb", postgresql.JSONB(), nullable=False),
        sa.Column(
            "ingest_batch_id",
            sa.Uuid(),
            sa.ForeignKey("ingest_batches.id"),
            nullable=False,
        ),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("replay_id", sa.Uuid(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_usage_ingest_attempts_source_identity_source_record",
        "usage_ingest_attempts",
        ["source_identity_id", "original_source_record_id"],
    )
    op.create_index(
        "ix_usage_ingest_attempts_usage_event_id",
        "usage_ingest_attempts",
        ["usage_event_id"],
    )
    op.create_index(
        "ix_usage_ingest_attempts_ingest_batch_id",
        "usage_ingest_attempts",
        ["ingest_batch_id"],
    )

    # ── source_identity_quarantine ───────────────────────────────────
    # resolution_id (circular FK -> source_identity_resolutions) is added
    # after both tables exist.
    op.create_table(
        "source_identity_quarantine",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "source_identity_id",
            sa.Uuid(),
            sa.ForeignKey("source_identities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "overlapping_identity_id",
            sa.Uuid(),
            sa.ForeignKey("source_identities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("overlap_count", sa.Integer(), nullable=False),
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "ix_source_identity_quarantine_identity_cleared",
        "source_identity_quarantine",
        ["source_identity_id", "cleared_at"],
    )

    # ── source_identity_resolutions ──────────────────────────────────
    # quarantine_id (circular FK -> source_identity_quarantine) is added
    # after both tables exist.
    op.create_table(
        "source_identity_resolutions",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("quarantine_id", sa.Uuid(), nullable=False),
        sa.Column(
            "resolving_identity_id",
            sa.Uuid(),
            sa.ForeignKey("source_identities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resolved_by_user_id", sa.String(), nullable=True),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
    )

    # ── circular FK pair (both tables now exist) ─────────────────────
    # Constraint names follow the ORM naming convention used by the other
    # FKs in this migration (fk_<table>_<column>_<reftable>) and stay under
    # PostgreSQL's 63-character identifier limit.
    op.create_foreign_key(
        "fk_source_identity_quarantine_resolution_id_resolutions",
        "source_identity_quarantine",
        "source_identity_resolutions",
        ["resolution_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_source_identity_resolutions_quarantine_id_quarantine",
        "source_identity_resolutions",
        "source_identity_quarantine",
        ["quarantine_id"],
        ["id"],
    )


def downgrade() -> None:
    """Drop the five canonical event schema tables in reverse dependency order."""
    # Circular FKs must be dropped before either table is dropped.
    op.drop_constraint(
        "fk_source_identity_resolutions_quarantine_id_quarantine",
        "source_identity_resolutions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_source_identity_quarantine_resolution_id_resolutions",
        "source_identity_quarantine",
        type_="foreignkey",
    )
    op.drop_table("usage_ingest_attempts")
    op.drop_table("usage_events")
    op.drop_table("source_identity_resolutions")
    op.drop_table("source_identity_quarantine")
    op.drop_table("source_identities")
