"""Add execution-transcript observability tables (issue #217, ADR 0016).

Before 0029, the Gateway modeled *summary* questions — usage aggregates,
session summaries, todo snapshots — but could not answer *reconstruction*
questions: what a subagent actually did, which tools were called in what
order, and how parent/child sessions branched.  The underlying OpenCode
runtime already stores that data in its SQLite ``message`` / ``part``
tables; nothing collected or modeled it.

0029 adds three append-oriented transcript tables plus a normalized
tool-call projection, strictly additive and never touching the usage slice
(``usage_events``, the Client Project Rollup, or the ``sessions``
aggregate):

* ``observed_messages`` — one OpenCode ``message`` row, keyed by
  ``(client_id, source_database_id, external_message_id)``; promoted
  columns ``role`` (NOT NULL), ``agent``, ``mode``, ``cost_usd``,
  ``input_tokens``/``output_tokens``, ``parent_external_session_id``
  (from ``message.data.parentID``); verbatim redacted ``data`` JSONB.
* ``observed_parts`` — one OpenCode ``part`` row, keyed by
  ``(client_id, source_database_id, external_part_id)``; the single
  promoted column ``part_type`` (NOT NULL); verbatim redacted ``data``.
* ``observed_tool_calls`` — one normalized tool-call row per tool part,
  keyed by ``(client_id, source_database_id, external_part_id)``;
  ``part_id`` (NOT NULL, refs ``observed_parts``), ``tool_name``
  (NOT NULL), ``tool_status``, and truncated ``tool_input``/``tool_output``.

Session identity and parent/child linkage are *reused*, not duplicated: the
tables hang off ``sessions.id`` and reuse ``opencode_session_contexts``
linkage (a proposed fourth ``observed_sessions`` table is rejected by ADR
0016 Decision 1).

Revision ID: 0029
Revises:     0028
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
    """Create the three execution-transcript tables and their indexes."""
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
        "ix_observed_messages_session_created",
        "observed_messages",
        ["session_id", "source_created_at"],
    )
    op.create_index("ix_observed_messages_agent", "observed_messages", ["agent"])
    op.create_index(
        "ix_observed_messages_role_created",
        "observed_messages",
        ["role", "source_created_at"],
    )

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
            sa.ForeignKey("observed_messages.id"),
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
        "ix_observed_parts_session_created",
        "observed_parts",
        ["session_id", "source_created_at"],
    )
    op.create_index(
        "ix_observed_parts_message_created",
        "observed_parts",
        ["message_id", "source_created_at"],
    )
    op.create_index(
        "ix_observed_parts_session_type_created",
        "observed_parts",
        ["session_id", "part_type", "source_created_at"],
    )
    op.create_index(
        "ix_observed_parts_type_created",
        "observed_parts",
        ["part_type", "source_created_at"],
    )
    op.create_index(
        "ix_observed_parts_created",
        "observed_parts",
        ["source_created_at"],
    )

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
            sa.ForeignKey("observed_parts.id"),
            nullable=False,
        ),
        sa.Column("external_part_id", sa.String(), nullable=False),
        sa.Column(
            "message_id",
            sa.Uuid(),
            sa.ForeignKey("observed_messages.id"),
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
        "ix_observed_tool_calls_session_created",
        "observed_tool_calls",
        ["session_id", "source_created_at"],
    )
    op.create_index(
        "ix_observed_tool_calls_name_created",
        "observed_tool_calls",
        ["tool_name", "source_created_at"],
    )
    op.create_index(
        "ix_observed_tool_calls_status",
        "observed_tool_calls",
        ["tool_status"],
        postgresql_where=sa.text("tool_status IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop the three execution-transcript tables."""
    op.drop_table("observed_tool_calls")
    op.drop_table("observed_parts")
    op.drop_table("observed_messages")
