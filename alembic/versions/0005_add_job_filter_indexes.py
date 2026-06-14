"""Add indexes on gateway_jobs filter columns.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_gateway_jobs_status", "gateway_jobs", ["status"])
    op.create_index("ix_gateway_jobs_runner_id", "gateway_jobs", ["runner_id"])
    op.create_index(
        "ix_gateway_jobs_workflow_run_id", "gateway_jobs", ["workflow_run_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_gateway_jobs_workflow_run_id")
    op.drop_index("ix_gateway_jobs_runner_id")
    op.drop_index("ix_gateway_jobs_status")
