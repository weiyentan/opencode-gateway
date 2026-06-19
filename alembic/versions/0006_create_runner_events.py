"""Create runner_events table for runner status-change audit trail.

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-19

Previously, runner status changes were logged into ``job_events`` using a
sentinel zero-UUID ``job_id`` (``00000000-0000-0000-0000-000000000000``).
This migration introduces a dedicated ``runner_events`` table so that:

- ``job_events`` only references real jobs (no fake job IDs)
- Runner status transitions are recorded in their own domain table
- A FK to ``runners.id`` enforces referential integrity

Columns:
  id          — UUID primary key
  runner_id   — FK → runners.id (CASCADE on delete)
  event_type  — e.g. ``runner_status_offline``, ``runner_status_online``
  old_status  — previous runner status (nullable for initial events)
  new_status  — new runner status
  reason      — human-readable reason for the change
  created_at  — timestamp of the event
  metadata    — JSONB for extensibility (nullable)
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runner_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "runner_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("old_status", sa.Text(), nullable=True),
        sa.Column("new_status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runner_events")),
        sa.ForeignKeyConstraint(
            ["runner_id"],
            ["runners.id"],
            name=op.f("fk_runner_events_runner_id_runners"),
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_runner_events_runner_id",
        "runner_events",
        ["runner_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_runner_events_runner_id", table_name="runner_events")
    op.drop_table("runner_events")
