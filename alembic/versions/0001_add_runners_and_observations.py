"""add runners and observation tables

Revision ID: 0001
Revises:
Create Date: 2026-06-11 00:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = "0000"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- runners ---------------------------------------------------------------
    op.create_table(
        "runners",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("runner_id", sa.Text(), nullable=False),
        sa.Column("hostname", sa.Text(), nullable=False),
        sa.Column("executor_type", sa.Text(), nullable=False),
        sa.Column("labels", postgresql.JSONB(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="UNKNOWN"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runners")),
        sa.UniqueConstraint("runner_id", name=op.f("uq_runners_runner_id")),
    )

    # -- runner_observations ---------------------------------------------------
    op.create_table(
        "runner_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "runner_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("disk_used_percent", sa.Float(), nullable=True),
        sa.Column("memory_used_percent", sa.Float(), nullable=True),
        sa.Column("load_1m", sa.Float(), nullable=True),
        sa.Column("load_5m", sa.Float(), nullable=True),
        sa.Column("load_15m", sa.Float(), nullable=True),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runner_observations")),
        sa.ForeignKeyConstraint(
            ["runner_id"],
            ["runners.id"],
            name=op.f("fk_runner_observations_runner_id_runners"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("id", name=op.f("uq_runner_observations_id")),
    )
    op.create_index(
        "ix_runner_observations_runner_observed",
        "runner_observations",
        ["runner_id", sa.text("observed_at DESC")],
    )

    # -- workspace_observations ------------------------------------------------
    op.create_table(
        "workspace_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "runner_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("workspace_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("opencode_status", sa.Text(), nullable=True),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace_observations")),
        sa.ForeignKeyConstraint(
            ["runner_id"],
            ["runners.id"],
            name=op.f("fk_workspace_observations_runner_id_runners"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("id", name=op.f("uq_workspace_observations_id")),
    )
    op.create_index(
        "ix_workspace_observations_runner_observed",
        "workspace_observations",
        ["runner_id", sa.text("observed_at DESC")],
    )

    # -- opencode_instance_observations ----------------------------------------
    op.create_table(
        "opencode_instance_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "runner_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("instance_name", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_opencode_instance_observations")),
        sa.ForeignKeyConstraint(
            ["runner_id"],
            ["runners.id"],
            name=op.f("fk_opencode_instance_observations_runner_id_runners"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("id", name=op.f("uq_opencode_instance_observations_id")),
    )
    op.create_index(
        "ix_opencode_instance_obs_runner_observed",
        "opencode_instance_observations",
        ["runner_id", sa.text("observed_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_opencode_instance_obs_runner_observed",
        table_name="opencode_instance_observations",
    )
    op.drop_table("opencode_instance_observations")
    op.drop_index(
        "ix_workspace_observations_runner_observed",
        table_name="workspace_observations",
    )
    op.drop_table("workspace_observations")
    op.drop_index(
        "ix_runner_observations_runner_observed",
        table_name="runner_observations",
    )
    op.drop_table("runner_observations")
    op.drop_table("runners")
