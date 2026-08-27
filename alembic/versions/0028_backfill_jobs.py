"""Add the durable AFK backfill job queue (API-triggered AFK backfill).

``afk_backfill_jobs`` is the durable queue backing the authenticated backfill
API (``/api/v1/backfill/*``) and its dedicated worker
(``python -m app.backfill.worker``).  One row per submitted job — dry-runs are
recorded as immediately-``completed`` rows so every run keeps an audit trail —
with the state machine

    queued -> running -> completed
                     -> failed
    queued -> cancelled

Write jobs are enqueued by the API and claimed by the worker with
``FOR UPDATE SKIP LOCKED``; per-repository serialization is enforced at the
worker layer with a session-level advisory lock keyed on
(provider, repository), never by a schema constraint.

Retention contract: completed/failed/cancelled rows are retained for 90 days
(``GATEWAY_BACKFILL_RETENTION_DAYS``) and then pruned by the worker's
retention sweep.  The ``completed_at`` partial index keeps the sweep cheap.

Additive only — no existing table is touched.  Provider credentials are never
stored: ``requested_by`` records a caller/key label, never a key value.

Revision ID: 0028
Revises:     0027
Create Date: 2026-08-20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the durable backfill job queue (additive)."""
    op.create_table(
        "afk_backfill_jobs",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("repository", sa.String(), nullable=False),
        sa.Column("window_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "show_evidence", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("requested_by", sa.String(), nullable=False),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failure_category", sa.String(), nullable=True),
        sa.Column("failure_message", sa.String(), nullable=True),
        sa.Column("evidence", postgresql.JSONB(), nullable=True),
        sa.Column("change_requests_scanned", sa.Integer(), nullable=True),
        sa.Column("issues_scanned", sa.Integer(), nullable=True),
        sa.Column("sessions_considered", sa.Integer(), nullable=True),
        sa.Column("explicit_matches", sa.Integer(), nullable=True),
        sa.Column("high_matches", sa.Integer(), nullable=True),
        sa.Column("inferred_matches", sa.Integer(), nullable=True),
        sa.Column("ambiguous", sa.Integer(), nullable=True),
        sa.Column("unmatched", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_afk_backfill_jobs_status",
        ),
    )
    op.create_index(
        "ix_afk_backfill_jobs_status_created",
        "afk_backfill_jobs",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_afk_backfill_jobs_repo_status_created",
        "afk_backfill_jobs",
        ["provider", "repository", "status", "created_at"],
    )
    op.create_index(
        "ix_afk_backfill_jobs_completed_at",
        "afk_backfill_jobs",
        ["completed_at"],
        postgresql_where=sa.text("completed_at IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop the backfill job queue."""
    op.drop_index("ix_afk_backfill_jobs_completed_at", table_name="afk_backfill_jobs")
    op.drop_index("ix_afk_backfill_jobs_repo_status_created", table_name="afk_backfill_jobs")
    op.drop_index("ix_afk_backfill_jobs_status_created", table_name="afk_backfill_jobs")
    op.drop_table("afk_backfill_jobs")
