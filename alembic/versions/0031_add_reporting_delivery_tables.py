"""Add the reporting-ingestion delivery tables (issue #479).

Creates two additive tables — ``reporting_deliveries`` and
``delivery_state_trails`` — without touching any existing table
(``usage_events``, ``opencode_usage_records``, ``sessions``,
``engineering_events``, ``delivery_log``, the transcript tables, etc.).

These tables back the normalized-event reporting ingestion surface
(cross-repo PRD #478): the producer (``fast-api-eda-gateway``) emits
``afk.events`` messages carrying ``event_type: "normalized"``; the Gateway
persists each delivery here.  The ``app.api.reporting_ingest`` endpoint is
the only writer.

The ``reporting_*`` table family is deliberately distinct from the AFK
outcome tables (``delivery_log`` / ``engineering_events``, migration 0026)
so the "only writer" contract owned by ``afk_outcomes.repository`` is
preserved by construction: this write path never touches those tables.

Write-semantics contract (enforced by the endpoint, guaranteed by the
constraints below):

* **Delivery dedup** — ``reporting_deliveries`` is keyed by
  ``UNIQUE (provider, delivery_id)`` and written with
  ``ON CONFLICT (provider, delivery_id) DO NOTHING`` so a redelivered
  message is absorbed (outcome ``duplicate``) rather than duplicated.
* **State trail** — ``delivery_state_trails`` is keyed by
  ``UNIQUE (provider, delivery_id, state, occurred_at)`` and written with
  ``ON CONFLICT DO NOTHING`` so an identical redelivered message appends
  nothing.  ``occurred_at`` is the *message's* timestamp (deterministic
  dedup anchor across redeliveries); ``state`` is bounded to
  ``persisted`` / ``rejected`` (the first-delivery outcome).  The trail
  carries no FK to ``reporting_deliveries`` (a loose reference, mirroring
  ``afk_run_sessions``) so it stays writable regardless of the delivery
  insert outcome.
* **No locked vocabulary** — ``event_type`` is stored verbatim as an
  opaque string; no type mapping is performed at this layer.

Pure conflict-ignore inserts need no advisory lock (there is no
read-modify-write), unlike the AFK enrich-only paths.

Revision ID: 0031
Revises:     0030
Create Date: 2026-08-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the two reporting-ingestion tables (additive)."""

    # ── reporting_deliveries ──────────────────────────────────────────
    op.create_table(
        "reporting_deliveries",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("delivery_id", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column(
            "received_at",
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
        sa.UniqueConstraint(
            "provider",
            "delivery_id",
            name="uq_reporting_deliveries_provider_delivery",
        ),
    )
    op.create_index(
        "ix_reporting_deliveries_received_at",
        "reporting_deliveries",
        ["received_at"],
    )

    # ── delivery_state_trails ─────────────────────────────────────────
    op.create_table(
        "delivery_state_trails",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("delivery_id", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "provider",
            "delivery_id",
            "state",
            "occurred_at",
            name="uq_delivery_state_trails_delivery_state_time",
        ),
    )
    op.create_index(
        "ix_delivery_state_trails_delivery",
        "delivery_state_trails",
        ["provider", "delivery_id"],
    )


def downgrade() -> None:
    """Drop the two reporting-ingestion tables in reverse dependency order."""
    op.drop_table("delivery_state_trails")
    op.drop_table("reporting_deliveries")
