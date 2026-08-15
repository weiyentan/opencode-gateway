"""Add occurred_at/ingested_at and the current-aggregate table (issue #480).

Builds the forward-only current-aggregate layer on top of the #479
reporting-delivery tables:

(a) ``reporting_deliveries.occurred_at`` — the provider (message) time.
    The column is added nullable first, backfilled from the matching
    ``delivery_state_trails.occurred_at`` (deterministic: earliest trail
    per delivery), then any unmatched row falls back to ``received_at``,
    and finally the column is made NOT NULL.  The ordering (backfill
    before NOT NULL) is load-bearing — a NOT NULL add on a populated
    table without a backfill would fail.
(b) ``reporting_deliveries.ingested_at`` — the gateway time, ``now()``.
(c) ``delivery_state_trails.ingested_at`` — so every event row carries
    both ``occurred_at`` and ``ingested_at`` (``received_at``/
    ``created_at`` are not silently reused).
(d) ``reporting_resource_aggregates`` — the current aggregate per stable
    resource identity ``(provider, repository_url, resource_type,
    resource_number)`` with a named UNIQUE constraint and a provider/URL
    secondary index.

Delivery-row immutability and the "only writer" contract are preserved:
this migration only adds columns to the #479 tables and creates a new
table; it never touches ``delivery_log`` / ``engineering_events`` / afk
tables or any migration 0000-0031.

Revision ID: 0032
Revises:     0031
Create Date: 2026-08-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the aggregate columns/table (additive; backfill before NOT NULL)."""

    # ── (a) reporting_deliveries.occurred_at — backfill then NOT NULL ────
    op.add_column(
        "reporting_deliveries",
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE reporting_deliveries rd
        SET occurred_at = trail.occurred_at
        FROM (
            SELECT DISTINCT ON (provider, delivery_id)
                   provider, delivery_id, occurred_at
            FROM delivery_state_trails
            ORDER BY provider, delivery_id, occurred_at
        ) trail
        WHERE rd.provider = trail.provider
          AND rd.delivery_id = trail.delivery_id
          AND rd.occurred_at IS NULL
        """
    )
    op.execute(
        "UPDATE reporting_deliveries "
        "SET occurred_at = received_at WHERE occurred_at IS NULL"
    )
    op.alter_column("reporting_deliveries", "occurred_at", nullable=False)

    # ── (b) reporting_deliveries.ingested_at ─────────────────────────────
    op.add_column(
        "reporting_deliveries",
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ── (c) delivery_state_trails.ingested_at ────────────────────────────
    op.add_column(
        "delivery_state_trails",
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # ── (d) reporting_resource_aggregates ────────────────────────────────
    op.create_table(
        "reporting_resource_aggregates",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("repository_url", sa.String(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_number", sa.String(), nullable=False),
        sa.Column("last_occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_delivery_id", sa.String(), nullable=False),
        sa.Column(
            "last_ingested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "key_provenance",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "provider",
            "repository_url",
            "resource_type",
            "resource_number",
            name="uq_reporting_resource_aggregates_identity",
        ),
    )
    op.create_index(
        "ix_reporting_resource_aggregates_provider_url",
        "reporting_resource_aggregates",
        ["provider", "repository_url"],
    )


def downgrade() -> None:
    """Drop the aggregate table and columns in reverse dependency order."""
    op.drop_index(
        "ix_reporting_resource_aggregates_provider_url",
        table_name="reporting_resource_aggregates",
    )
    op.drop_column("reporting_resource_aggregates", "key_provenance")
    op.drop_table("reporting_resource_aggregates")
    op.drop_column("delivery_state_trails", "ingested_at")
    op.drop_column("reporting_deliveries", "ingested_at")
    op.drop_column("reporting_deliveries", "occurred_at")
