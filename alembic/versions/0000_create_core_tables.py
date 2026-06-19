"""Create core tables: gateway_jobs, workspaces, job_events, approvals.

Revision ID: 0000
Revises: (none — root migration)
Create Date: 2026-06-19 00:00:00.000000

This is the baseline migration that creates the four core domain tables
previously managed only by schema.sql.  Subsequent migrations 0003–0005
add additional columns (env_vars, branch_name, mr_url, workflow_run_id)
and indexes to gateway_jobs.

For existing databases that already have these tables (created by the
old schema.sql path), stamp this revision as applied:

    alembic stamp 0000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0000"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- gateway_jobs ----------------------------------------------------------
    op.create_table(
        "gateway_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("repo_url", sa.Text(), nullable=False),
        sa.Column("task_summary", sa.Text(), nullable=False),
        sa.Column("runner_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("workspace_name", sa.Text(), nullable=True),
        sa.Column("opencode_url", sa.Text(), nullable=True),
        sa.Column("opencode_session_id", sa.Text(), nullable=True),
        sa.Column("executor_type", sa.Text(), nullable=False),
        sa.Column("executor_job_id", sa.Text(), nullable=True),
        sa.Column("diff", sa.Text(), nullable=True),
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
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gateway_jobs")),
    )

    # -- workspaces ------------------------------------------------------------
    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("runner_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("workspace_name", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("repo_url", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("service_name", sa.Text(), nullable=True),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("cleanup_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cleanup_status",
            sa.Text(),
            nullable=False,
            server_default="active",
        ),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
    )

    # -- job_events ------------------------------------------------------------
    op.create_table(
        "job_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("previous_status", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_events")),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["gateway_jobs.id"],
            name=op.f("fk_job_events_job_id_gateway_jobs"),
        ),
    )

    # -- approvals -------------------------------------------------------------
    op.create_table(
        "approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("requested_action", sa.Text(), nullable=False),
        sa.Column("approval_type", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["gateway_jobs.id"],
            name=op.f("fk_approvals_job_id_gateway_jobs"),
        ),
    )


def downgrade() -> None:
    op.drop_table("approvals")
    op.drop_table("job_events")
    op.drop_table("workspaces")
    op.drop_table("gateway_jobs")
