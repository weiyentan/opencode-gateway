"""Add OpenCode source-fact projection tables.

Creates: opencode_session_contexts, opencode_source_projects,
opencode_project_directories, opencode_session_todos.

These tables store denormalised projections of OpenCode source data
extracted from collector payloads during ingest.

Revision ID: 0015
Revises:     0014
Create Date: 2026-07-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the four OpenCode source-fact projection tables."""

    # ── opencode_source_projects ─────────────────────────────────────
    op.create_table(
        "opencode_source_projects",
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
        sa.Column("external_project_id", sa.String(), nullable=False),
        sa.Column("source_project_id", sa.Uuid(), nullable=True),
        sa.Column("worktree", sa.String(), nullable=True),
        sa.Column("vcs", sa.String(), nullable=True),
        sa.Column("sandboxes", postgresql.JSONB(), nullable=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("icon", sa.String(), nullable=True),
        sa.Column("icon_color", sa.String(), nullable=True),
        sa.Column("raw_commands", sa.String(), nullable=True),
        sa.Column("parsed_commands", postgresql.JSONB(), nullable=True),
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
        sa.Column("source_payload", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint(
            "client_id",
            "source_database_id",
            "external_project_id",
            name="uq_opencode_source_projects_source_key",
        ),
    )

    # ── opencode_session_contexts ────────────────────────────────────
    op.create_table(
        "opencode_session_contexts",
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
        sa.Column("external_session_id", sa.String(), nullable=False),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("sessions.id"),
            nullable=True,
        ),
        sa.Column("parent_external_session_id", sa.String(), nullable=True),
        sa.Column(
            "parent_session_id",
            sa.Uuid(),
            sa.ForeignKey("sessions.id"),
            nullable=True,
        ),
        sa.Column("external_project_id", sa.String(), nullable=True),
        sa.Column(
            "source_project_id",
            sa.Uuid(),
            sa.ForeignKey("opencode_source_projects.id"),
            nullable=True,
        ),
        sa.Column("source_directory", sa.String(), nullable=True),
        sa.Column("source_path", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("slug", sa.String(), nullable=True),
        sa.Column("version", sa.String(), nullable=True),
        sa.Column("session_model", sa.String(), nullable=True),
        sa.Column("session_cost", sa.Numeric(), nullable=True),
        sa.Column("source_input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("source_output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("source_cached_tokens", sa.BigInteger(), nullable=True),
        sa.Column("source_reasoning_tokens", sa.BigInteger(), nullable=True),
        sa.Column("code_change_count", sa.Integer(), nullable=True),
        sa.Column("code_change_additions", sa.Integer(), nullable=True),
        sa.Column("code_change_deletions", sa.Integer(), nullable=True),
        sa.Column("source_created_at", sa.BigInteger(), nullable=True),
        sa.Column("source_updated_at", sa.BigInteger(), nullable=True),
        sa.Column("source_started_at", sa.BigInteger(), nullable=True),
        sa.Column("source_completed_at", sa.BigInteger(), nullable=True),
        sa.Column("source_created_at_tz", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_updated_at_tz", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_started_at_tz", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_completed_at_tz", sa.DateTime(timezone=True), nullable=True),
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
        sa.Column("source_payload", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint(
            "client_id",
            "source_database_id",
            "external_session_id",
            name="uq_opencode_session_contexts_source_key",
        ),
    )

    # ── opencode_project_directories ─────────────────────────────────
    op.create_table(
        "opencode_project_directories",
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
        sa.Column("directory", sa.String(), nullable=False),
        sa.Column("directory_type", sa.String(), nullable=True),
        sa.Column("strategy", sa.String(), nullable=True),
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
        sa.Column("source_payload", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint(
            "client_id",
            "source_database_id",
            "directory",
            name="uq_opencode_project_directories_source_key",
        ),
    )

    # ── opencode_session_todos ───────────────────────────────────────
    op.create_table(
        "opencode_session_todos",
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
        sa.Column("external_session_id", sa.String(), nullable=False),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("sessions.id"),
            nullable=True,
        ),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("priority", sa.String(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=True),
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
        sa.Column("source_payload", postgresql.JSONB(), nullable=True),
        sa.UniqueConstraint(
            "client_id",
            "source_database_id",
            "external_session_id",
            "position",
            name="uq_opencode_session_todos_source_key",
        ),
    )


def downgrade() -> None:
    """Drop the four OpenCode source-fact projection tables."""
    op.drop_table("opencode_session_todos")
    op.drop_table("opencode_project_directories")
    op.drop_table("opencode_session_contexts")
    op.drop_table("opencode_source_projects")
