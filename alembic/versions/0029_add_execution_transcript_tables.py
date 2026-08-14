"""Add execution-transcript projection tables (issue #464).

Creates three additive tables — ``observed_messages``, ``observed_parts``,
``observed_tool_calls`` — without touching any existing usage, aggregate,
or projection table (``usage_events``, ``opencode_usage_records``,
``sessions``, ``source_identities``, ``client_project_rollup``, the ADR
0008 projections, the AFK outcome tables, etc.).

These tables are the Layer-0 schema foundation of the execution-transcript
slice (ADR 0016, issue #217): an append-only, chronologically-ordered
stream of OpenCode ``message`` / ``part`` rows projected into Gateway-owned
tables, plus a normalized tool-call query surface.

Schema conventions follow ADR 0008 / migration 0015 projections:

* ``client_id`` / ``source_database_id`` ownership, external identity
  columns, and a resolved internal ``session_id`` referencing
  ``sessions(id)``.
* Dual source timestamps — raw millisecond ``bigint``
  (``source_created_at`` / ``source_updated_at``) plus normalized
  ``timestamptz`` (``source_created_at_tz`` / ``source_updated_at_tz``).
* ``first_seen_at`` / ``last_seen_at`` (server default ``now()``).
* Verbatim ``data`` JSONB holding the full redacted source payload.

Identity / upsert keys (idempotent ``ON CONFLICT`` at ingest):

* ``observed_messages`` — UNIQUE (client_id, source_database_id,
  external_message_id).  Promoted columns: ``role``, ``agent``, ``mode``,
  ``cost_usd``, ``input_tokens``, ``output_tokens``, and
  ``parent_external_session_id`` (normalized from ``message.data.parentID``,
  a reconstruction safety net for out-of-order Session Context).
* ``observed_parts`` — UNIQUE (client_id, source_database_id,
  external_part_id).  ``part_type`` is the only promoted field; all content
  (text, reasoning, tool payloads, step markers) stays in ``data``.
  Chronological order via ``source_created_at`` with ``id`` tiebreaker.
* ``observed_tool_calls`` — UNIQUE (client_id, source_database_id,
  external_part_id), one row per tool part.  ``part_id`` is NOT NULL and
  references ``observed_parts(id)``; ``tool_input`` / ``tool_output`` are
  stored truncated (truncation happens at ingest, never here).

Foreign keys within this migration's new tables use ``ondelete="CASCADE"``
(children are cleaned up with their parent), mirroring the 0021/0026
convention for FKs within a single migration's new tables;
``session_id`` references ``sessions(id)`` with no delete action, matching
migration 0015.

Indexes follow ADR 0016 §5: each unique key plus the composite
timeline/filter set, and a partial ``tool_status`` index on
``observed_tool_calls``.

Revision ID: 0029
Revises:     0028
Create Date: 2026-08-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the three execution-transcript tables (additive)."""

    # ── observed_messages ─────────────────────────────────────────────
    op.create_table(
        "observed_messages",
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
            sa.ForeignKey("source_databases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_message_id", sa.String(), nullable=False),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("sessions.id"),
            nullable=True,
        ),
        sa.Column("external_session_id", sa.String(), nullable=False),
        sa.Column("parent_external_session_id", sa.String(), nullable=True),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("agent", sa.String(), nullable=True),
        sa.Column("mode", sa.String(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(), nullable=True),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("source_created_at", sa.BigInteger(), nullable=True),
        sa.Column("source_updated_at", sa.BigInteger(), nullable=True),
        sa.Column("source_created_at_tz", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_updated_at_tz", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column("data", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint(
            "client_id",
            "source_database_id",
            "external_message_id",
            name="uq_observed_messages_source_key",
        ),
    )
    op.create_index(
        "ix_observed_messages_session_id_source_created_at",
        "observed_messages",
        ["session_id", "source_created_at"],
    )
    op.create_index(
        "ix_observed_messages_agent",
        "observed_messages",
        ["agent"],
    )
    op.create_index(
        "ix_observed_messages_role_source_created_at",
        "observed_messages",
        ["role", "source_created_at"],
    )

    # ── observed_parts ────────────────────────────────────────────────
    op.create_table(
        "observed_parts",
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
            sa.ForeignKey("source_databases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_part_id", sa.String(), nullable=False),
        sa.Column(
            "message_id",
            sa.Uuid(),
            sa.ForeignKey("observed_messages.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("external_message_id", sa.String(), nullable=False),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("sessions.id"),
            nullable=True,
        ),
        sa.Column("external_session_id", sa.String(), nullable=False),
        sa.Column("part_type", sa.String(), nullable=False),
        sa.Column("source_created_at", sa.BigInteger(), nullable=True),
        sa.Column("source_updated_at", sa.BigInteger(), nullable=True),
        sa.Column("source_created_at_tz", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_updated_at_tz", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column("data", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint(
            "client_id",
            "source_database_id",
            "external_part_id",
            name="uq_observed_parts_source_key",
        ),
    )
    op.create_index(
        "ix_observed_parts_session_id_source_created_at",
        "observed_parts",
        ["session_id", "source_created_at"],
    )
    op.create_index(
        "ix_observed_parts_message_id_source_created_at",
        "observed_parts",
        ["message_id", "source_created_at"],
    )
    op.create_index(
        "ix_observed_parts_session_id_part_type_source_created_at",
        "observed_parts",
        ["session_id", "part_type", "source_created_at"],
    )
    op.create_index(
        "ix_observed_parts_part_type_source_created_at",
        "observed_parts",
        ["part_type", "source_created_at"],
    )
    op.create_index(
        "ix_observed_parts_source_created_at",
        "observed_parts",
        ["source_created_at"],
    )

    # ── observed_tool_calls ───────────────────────────────────────────
    op.create_table(
        "observed_tool_calls",
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
            sa.ForeignKey("source_databases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "part_id",
            sa.Uuid(),
            sa.ForeignKey("observed_parts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_part_id", sa.String(), nullable=False),
        sa.Column(
            "message_id",
            sa.Uuid(),
            sa.ForeignKey("observed_messages.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("sessions.id"),
            nullable=True,
        ),
        sa.Column("external_session_id", sa.String(), nullable=False),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("tool_status", sa.String(), nullable=True),
        sa.Column("tool_input", postgresql.JSONB(), nullable=True),
        sa.Column("tool_output", postgresql.JSONB(), nullable=True),
        sa.Column("source_created_at", sa.BigInteger(), nullable=True),
        sa.Column("source_updated_at", sa.BigInteger(), nullable=True),
        sa.Column("source_created_at_tz", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_updated_at_tz", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column("data", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint(
            "client_id",
            "source_database_id",
            "external_part_id",
            name="uq_observed_tool_calls_source_key",
        ),
    )
    op.create_index(
        "ix_observed_tool_calls_session_id_source_created_at",
        "observed_tool_calls",
        ["session_id", "source_created_at"],
    )
    op.create_index(
        "ix_observed_tool_calls_tool_name_source_created_at",
        "observed_tool_calls",
        ["tool_name", "source_created_at"],
    )
    op.create_index(
        "ix_observed_tool_calls_tool_status",
        "observed_tool_calls",
        ["tool_status"],
        postgresql_where=sa.text("tool_status IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop the three execution-transcript tables in reverse dependency order."""
    op.drop_table("observed_tool_calls")
    op.drop_table("observed_parts")
    op.drop_table("observed_messages")
