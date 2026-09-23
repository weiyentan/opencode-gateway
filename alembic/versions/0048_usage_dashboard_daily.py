"""Add the usage_dashboard_daily pre-aggregated read model (issue #735).

Creates: usage_dashboard_daily.

A pre-aggregated operational read-model for the usage dashboard, keyed by
``(day, provider)``.  Follows the ``afk_dashboard_daily`` precedent
(migration 0046, issue #714) and the ``client_project_rollup`` precedent
(migration 0023, ADR 0014/0015): a composite primary key on the bucket
identity, additive-only metric columns, and a reversible downgrade.

Each row stores only additive usage metrics for one UTC day and one
provider bucket — token totals, estimated cost, and record count.  No
non-additive (distinct-count, ratio, or percentile) values are stored:
those cannot be summed across buckets and remain queries over the
canonical ``usage_events`` source table.

The composite primary key on ``(day, provider)`` serves the date-range
scans of the unfiltered summary, and the ``(provider, day)`` index
serves provider-scoped scans.  Every additive column is ``NOT NULL
DEFAULT 0`` so a freshly recomputed bucket never needs to distinguish
"missing" from zero.

Runtime access is raw asyncpg (the recomputation engine is the only
writer); the SQLAlchemy model exists for Alembic autogenerate only.

Revision ID: 0048
Revises:     0047
Create Date: 2026-09-23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0048"
down_revision: Union[str, None] = "0047"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the additive usage dashboard daily rollup table."""
    op.create_table(
        "usage_dashboard_daily",
        # ── Bucket identity — composite primary key ──
        sa.Column("day", sa.Date(), primary_key=True, nullable=False),
        sa.Column("provider", sa.String(), primary_key=True, nullable=False),
        # ── Additive usage metrics ──
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
            "reasoning_tokens",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "cached_tokens",
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
        sa.Column(
            "record_count",
            sa.Integer(),
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
    # Provider-scoped day-range scans of the usage dashboard read path;
    # the composite primary key already covers unfiltered date-range scans.
    op.create_index(
        "ix_usage_dashboard_daily_provider_day",
        "usage_dashboard_daily",
        ["provider", "day"],
    )
    # Unfiltered day-range scans (e.g. the shared dashboard date range).
    op.create_index(
        "ix_usage_dashboard_daily_day",
        "usage_dashboard_daily",
        ["day"],
    )


def downgrade() -> None:
    """Drop the usage_dashboard_daily rollup table."""
    op.drop_index(
        "ix_usage_dashboard_daily_day",
        table_name="usage_dashboard_daily",
    )
    op.drop_index(
        "ix_usage_dashboard_daily_provider_day",
        table_name="usage_dashboard_daily",
    )
    op.drop_table("usage_dashboard_daily")
