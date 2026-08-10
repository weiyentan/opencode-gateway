"""Add nullable canonical_name column to opencode_clients.

Per-workspace client registrations are assigned a shared canonical name so
their usage rolls up under a single deployment identity.  The column is
nullable — existing clients are unaffected and report under their own name
when canonical_name IS NULL.  No backfill is needed (new column, null
default).

See: ADR 0014 — Canonical Client Name

Revision ID: 0022
Revises:     0021
Create Date: 2026-08-09
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the canonical_name column to opencode_clients."""
    op.add_column(
        "opencode_clients",
        sa.Column("canonical_name", sa.String(), nullable=True),
    )


def downgrade() -> None:
    """Drop the canonical_name column from opencode_clients."""
    op.drop_column("opencode_clients", "canonical_name")
