"""Observability domain tables for usage ingest.

Creates: source_databases, observed_models, sessions,
opencode_usage_records, ingest_batches, ingest_audit.

Revision ID: 0012
Revises:     0001
Create Date: 2025-07-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the six observability domain tables."""

    # ── source_databases ─────────────────────────────────────────────
    op.create_table(
        "source_databases",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "collector_credential_id",
            sa.Uuid(),
            sa.ForeignKey("collector_credentials.id"),
            nullable=False,
        ),
        sa.Column(
            "client_id",
            sa.Uuid(),
            sa.ForeignKey("opencode_clients.id"),
            nullable=False,
        ),
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
        sa.Column(
            "record_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )

    # ── observed_models ──────────────────────────────────────────────
    op.create_table(
        "observed_models",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("model_name", sa.String(), nullable=False, unique=True),
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

    # ── sessions ─────────────────────────────────────────────────────
    op.create_table(
        "sessions",
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
        sa.Column(
            "source_database_id",
            sa.Uuid(),
            sa.ForeignKey("source_databases.id"),
            nullable=False,
        ),
        sa.Column("external_session_id", sa.String(), nullable=True),
        sa.Column(
            "first_message_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_message_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "message_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "total_input_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "total_output_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "total_cached_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "total_estimated_cost_usd",
            sa.Numeric(),
            nullable=True,
        ),
    )

    # ── opencode_usage_records ───────────────────────────────────────
    op.create_table(
        "opencode_usage_records",
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
        sa.Column(
            "source_database_id",
            sa.Uuid(),
            sa.ForeignKey("source_databases.id"),
            nullable=False,
        ),
        sa.Column("source_record_id", sa.String(), nullable=False),
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
        sa.Column(
            "cached_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("estimated_cost_usd", sa.Numeric(), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "client_id",
            "source_database_id",
            "source_record_id",
            name="uq_opencode_usage_records_dedup",
        ),
    )

    # ── ingest_batches ───────────────────────────────────────────────
    op.create_table(
        "ingest_batches",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "collector_credential_id",
            sa.Uuid(),
            sa.ForeignKey("collector_credentials.id"),
            nullable=False,
        ),
        sa.Column(
            "client_id",
            sa.Uuid(),
            sa.ForeignKey("opencode_clients.id"),
            nullable=False,
        ),
        sa.Column("collector_version", sa.String(), nullable=True),
        sa.Column("schema_version", sa.String(), nullable=True),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("accepted_count", sa.Integer(), nullable=False),
        sa.Column("rejected_count", sa.Integer(), nullable=False),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ── ingest_audit ─────────────────────────────────────────────────
    op.create_table(
        "ingest_audit",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "ingest_batch_id",
            sa.Uuid(),
            sa.ForeignKey("ingest_batches.id"),
            nullable=False,
        ),
        sa.Column("record_index", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    """Drop the six observability domain tables in reverse dependency order."""
    op.drop_table("ingest_audit")
    op.drop_table("ingest_batches")
    op.drop_table("opencode_usage_records")
    op.drop_table("sessions")
    op.drop_table("observed_models")
    op.drop_table("source_databases")
