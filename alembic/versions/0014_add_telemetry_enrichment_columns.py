"""Add telemetry enrichment columns to sessions and opencode_usage_records.

Adds columns for v1.2 enrichment fields: project_id, workspace_id, agent,
parent_session_id to the sessions table, and provider, mode, finish_reason,
reasoning_tokens, cache_read_tokens, cache_write_tokens to the
opencode_usage_records table.

Revision ID: 0014
Revises:     0013
Create Date: 2025-07-26
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add enrichment columns to sessions and opencode_usage_records."""

    # ── sessions ──────────────────────────────────────────────────────────
    op.add_column("sessions", sa.Column("project_id", sa.String(), nullable=True))
    op.add_column("sessions", sa.Column("workspace_id", sa.String(), nullable=True))
    op.add_column("sessions", sa.Column("agent", sa.String(), nullable=True))
    op.add_column("sessions", sa.Column("parent_session_id", sa.String(), nullable=True))

    # ── opencode_usage_records ────────────────────────────────────────────
    op.add_column("opencode_usage_records", sa.Column("provider", sa.String(), nullable=True))
    op.add_column("opencode_usage_records", sa.Column("mode", sa.String(), nullable=True))
    op.add_column("opencode_usage_records", sa.Column("finish_reason", sa.String(), nullable=True))
    op.add_column(
        "opencode_usage_records",
        sa.Column("reasoning_tokens", sa.Integer(), nullable=True),
    )
    op.add_column(
        "opencode_usage_records",
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
    )
    op.add_column(
        "opencode_usage_records",
        sa.Column("cache_write_tokens", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Drop the enrichment columns in reverse order."""

    # ── opencode_usage_records (reverse order) ────────────────────────────
    op.drop_column("opencode_usage_records", "cache_write_tokens")
    op.drop_column("opencode_usage_records", "cache_read_tokens")
    op.drop_column("opencode_usage_records", "reasoning_tokens")
    op.drop_column("opencode_usage_records", "finish_reason")
    op.drop_column("opencode_usage_records", "mode")
    op.drop_column("opencode_usage_records", "provider")

    # ── sessions (reverse order) ──────────────────────────────────────────
    op.drop_column("sessions", "parent_session_id")
    op.drop_column("sessions", "agent")
    op.drop_column("sessions", "workspace_id")
    op.drop_column("sessions", "project_id")
