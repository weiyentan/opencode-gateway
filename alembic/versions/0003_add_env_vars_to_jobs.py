"""Add env_vars JSONB column to gateway_jobs

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-14 12:00:00.000000
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "gateway_jobs",
        sa.Column(
            "env_vars",
            postgresql.JSONB,
            nullable=True,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("gateway_jobs", "env_vars")
