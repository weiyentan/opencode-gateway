"""Initial clean baseline — no execution-era tables.

This migration is intentionally empty.  Future slices of the refactor
will add observability-specific tables in new migration files.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0000"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the initial Alembic baseline (intentionally empty)."""
    pass


def downgrade() -> None:
    """Revert the baseline (intentionally empty)."""
    pass
