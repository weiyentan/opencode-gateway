"""Retire the redundant afk_runs.status lifecycle column (issue #649).

Revision ID: 0045
Revises:     0044

ADR 0028 makes the PR/MR (change-request) lifecycle authoritative: the AFK
Run remains open while its change request is open and becomes terminal only
through change-request state.  The string ``afk_runs.status`` column is
redundant with that authority (plus ``outcome_status`` and the execution
binding outcomes) and always stayed at the provisional ``pending`` after
execution-binding writes (ADR 0028 supersedes ADR 0027's projection).  This
migration removes the column; every reader/writer (repository upsert,
provisioning INSERT, guarded update, API filters, and schemas) was retired
in the same change.

The downgrade restores the column with a server default so pre-retirement
code can run against a restored schema; historically stored values are not
recoverable (the column carried no semantics the change-request lifecycle
does not already cover).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "0045"
down_revision: Union[str, None] = "0044"  # noqa: UP007
branch_labels: Union[str, Sequence[str], None] = None  # noqa: UP007
depends_on: Union[str, Sequence[str], None] = None  # noqa: UP007


def upgrade() -> None:
    """Drop the redundant ``afk_runs.status`` lifecycle column."""
    op.execute("ALTER TABLE afk_runs DROP COLUMN IF EXISTS status")


def downgrade() -> None:
    """Restore ``afk_runs.status`` with a provisional server default."""
    op.add_column(
        "afk_runs",
        sa.Column(
            "status",
            sa.String(),
            nullable=False,
            server_default="pending",
        ),
    )
