"""Add the afk_dashboard_daily day index (issue #720).

The ``afk_dashboard_daily`` table is created by migration 0046 (PR #721,
issues #714/#715), keyed by ``(day, provider, repository)`` — the triple
the AFK Dashboard summary endpoint (issue #719,
``app/api/afk_dashboard_summary.py``) groups by.  This follow-up migration
aligns with that schema and does not create the table again; it only adds
the ``day`` index the summary endpoint needs.

The 0046 composite index
(``ix_afk_dashboard_daily_provider_repository_day``) serves provider-scoped
scans.  The summary endpoint also issues a date-range-only filter
(``day >= $1 AND day <= $2``) with no provider/repository predicate; a
dedicated ``day`` index keeps that scan efficient.

Revision ID: 0047
Revises:     0046
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0047"
down_revision: Union[str, None] = "0046"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the date-range index for the AFK Dashboard summary endpoint."""
    op.create_index(
        "ix_afk_dashboard_daily_day",
        "afk_dashboard_daily",
        ["day"],
    )


def downgrade() -> None:
    """Drop the date-range index added by this migration."""
    op.drop_index(
        "ix_afk_dashboard_daily_day",
        table_name="afk_dashboard_daily",
    )
