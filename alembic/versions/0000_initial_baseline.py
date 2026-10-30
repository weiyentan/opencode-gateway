"""Initial clean baseline — no execution-era tables.

This migration is intentionally empty.  Future slices of the refactor
will add observability-specific tables in new migration files.
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0000"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the initial Alembic baseline (intentionally empty)."""
    pass


def downgrade() -> None:
    """Revert the baseline (intentionally empty)."""
    pass
