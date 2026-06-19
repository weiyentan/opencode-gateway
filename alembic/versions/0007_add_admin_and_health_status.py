"""Add admin_status and health_status columns to runners table.

Migrate existing status values into the new columns:

- online, offline, maintenance → admin_status
- HEALTHY, BLOCKED_DISK_PRESSURE, BLOCKED_MEMORY_PRESSURE, UNKNOWN → health_status

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


MANUAL_STATUSES = frozenset({"online", "offline", "maintenance"})


def upgrade() -> None:
    # Step 1: Add nullable columns
    op.add_column("runners", sa.Column("admin_status", sa.Text(), nullable=True))
    op.add_column("runners", sa.Column("health_status", sa.Text(), nullable=True))

    # Step 2: Migrate existing data
    #   - manual statuses (online, offline, maintenance) → admin_status
    #   - observation-derived statuses → health_status
    conn = op.get_bind()
    for row in conn.execute(sa.text("SELECT id, status FROM runners")):
        runner_id = row[0]
        old_status = row[1]
        if old_status in MANUAL_STATUSES:
            conn.execute(
                sa.text(
                    "UPDATE runners SET admin_status = :status WHERE id = :id"
                ),
                {"status": old_status, "id": runner_id},
            )
        else:
            conn.execute(
                sa.text(
                    "UPDATE runners SET health_status = :status WHERE id = :id"
                ),
                {"status": old_status, "id": runner_id},
            )


def downgrade() -> None:
    op.drop_column("runners", "health_status")
    op.drop_column("runners", "admin_status")
