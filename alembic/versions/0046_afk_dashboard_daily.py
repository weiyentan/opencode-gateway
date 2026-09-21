"""Add the afk_dashboard_daily table (issue #720).

Creates: afk_dashboard_daily.

The pre-aggregated daily AFK-dashboard rollup (issue #714) read by the
AFK Dashboard summary endpoint (issue #719,
``app/api/afk_dashboard_summary.py``), keyed by
``(provider, repository, day)`` — the same triple the summary query
groups by.  Each row stores additive per-day totals: run / change-request
/ execution / session counts, the four token categories (input, output,
cache read, cache write), and the estimated cost.  ``derived_at`` is the
nullable freshness marker of the rollup recompute that produced the row
(``MAX(derived_at)`` across returned buckets becomes the summary's
``derived_at``).

Metric columns are NOT NULL with a zero server default so the rollup
writer can omit zero counters, mirroring the additive-counter convention
of the ``client_project_rollup`` table (migration 0023).  The summary
endpoint still ``COALESCE``s every ``SUM`` defensively.

The composite primary key serves the per-(provider, repository, day)
point lookups and UPSERTs of rollup maintenance; the ``day`` index serves
the summary's date-range filter (``day >= $1 AND day <= $2``), which
cannot use the primary key because ``day`` is its last column.

Revision ID: 0046
Revises:     0045
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
    """Create the additive per-(provider, repository, day) dashboard rollup."""
    op.create_table(
        "afk_dashboard_daily",
        sa.Column("provider", sa.Text(), primary_key=True, nullable=False),
        sa.Column("repository", sa.Text(), primary_key=True, nullable=False),
        sa.Column("day", sa.Date(), primary_key=True, nullable=False),
        sa.Column(
            "runs_started",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "change_requests_opened",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "change_requests_merged",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "change_requests_closed",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "execution_count",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "successful_execution_count",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "failed_execution_count",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "cancelled_execution_count",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "session_count",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "input_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "output_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "cache_read_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "cache_write_tokens",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "estimated_cost_usd",
            sa.Numeric(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("derived_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The summary endpoint's date-range filter (``day >= $1 AND day <= $2``)
    # cannot use the composite primary key — ``day`` is its last column.
    op.create_index(
        "ix_afk_dashboard_daily_day",
        "afk_dashboard_daily",
        ["day"],
    )


def downgrade() -> None:
    """Drop the afk_dashboard_daily table."""
    op.drop_index(
        "ix_afk_dashboard_daily_day",
        table_name="afk_dashboard_daily",
    )
    op.drop_table("afk_dashboard_daily")
