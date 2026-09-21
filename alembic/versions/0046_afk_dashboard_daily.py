"""Add the afk_dashboard_daily rollup table (issue #714).

Creates: afk_dashboard_daily.

A pre-aggregated operational read-model for the AFK dashboard, keyed by
``(day, provider, repository)``.  Follows the ``client_project_rollup``
precedent (migration 0023, ADR 0014/0015): a composite primary key on the
bucket identity, additive-only metric columns, and a reversible downgrade.

Each row stores only additive AFK metrics for one UTC day and one
provider/repository bucket — run counts, change-request counts, execution
counts, session count, token totals, and estimated cost.  No non-additive
(distinct-count, ratio, or percentile) values are stored: those cannot be
summed across buckets and remain queries over the canonical source tables.

The composite primary key on ``(day, provider, repository)`` serves the
date-range scans of the unfiltered summary, and the
``(provider, repository, day)`` index serves provider/repository-scoped
scans.  Every additive column is ``NOT NULL DEFAULT 0`` so a freshly
recomputed bucket never needs to distinguish "missing" from zero.

Runtime access is raw asyncpg (the recomputation engine is the only
writer); the SQLAlchemy model exists for Alembic autogenerate only.

Revision ID: 0046
Revises:     0045
Create Date: 2026-09-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0046"
down_revision: Union[str, None] = "0045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the additive AFK dashboard daily rollup table."""
    op.create_table(
        "afk_dashboard_daily",
        # ── Bucket identity — composite primary key ──
        sa.Column("day", sa.Date(), primary_key=True, nullable=False),
        sa.Column("provider", sa.String(), primary_key=True, nullable=False),
        sa.Column("repository", sa.String(), primary_key=True, nullable=False),
        # ── Additive AFK metrics ──
        sa.Column(
            "runs_started",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "change_requests_opened",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "change_requests_merged",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "change_requests_closed",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "execution_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "successful_execution_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "failed_execution_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "cancelled_execution_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "session_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "input_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "output_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "cache_read_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "cache_write_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "estimated_cost_usd",
            sa.Numeric(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        # ── Freshness / versioning metadata ──
        sa.Column(
            "derived_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("rollup_version", sa.String(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # Provider/repository-scoped day-range scans of the summary read path;
    # the composite primary key already covers unfiltered date-range scans.
    op.create_index(
        "ix_afk_dashboard_daily_provider_repository_day",
        "afk_dashboard_daily",
        ["provider", "repository", "day"],
    )


def downgrade() -> None:
    """Drop the afk_dashboard_daily rollup table."""
    op.drop_index(
        "ix_afk_dashboard_daily_provider_repository_day",
        table_name="afk_dashboard_daily",
    )
    op.drop_table("afk_dashboard_daily")
