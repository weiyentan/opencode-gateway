"""Add the client_project_rollup table.

Creates: client_project_rollup.

A pre-aggregated read-model of the canonical ``usage_events`` table (ADR
0015), keyed by ``(client_id, project_id, day)``.  ``project_id`` is the
stable project identifier, not the volatile display label — keying on the
label string would fragment the table every time a label changes (the
friendly-name backfill already rewrote labels once); the human-readable
label is resolved at read time from source project metadata, mirroring
the canonical-client-name COALESCE pattern.

Each row stores only additive token and cost totals — input, output,
cache read, cache write, and estimated cost.  No session counts, no model
counts, and no reasoning-token total: distinct counts do not aggregate
additively across days and remain distinct-count or sum queries over raw
``usage_events`` records.

The composite primary key on ``(client_id, project_id, day)`` serves the
per-(client, project, day) point lookups and UPSERTs of ingest-time
maintenance; the ``(client_id, day)`` index serves client-scoped day-range
scans of the hybrid read path (``group_by=client,project`` reads this
table; every other aggregate dimension keeps scanning ``usage_events``).
The foreign key toward the pre-existing ``opencode_clients`` table follows
the repo default of no ``ON DELETE`` action (mirroring 0021's FKs toward
pre-existing tables).

Revision ID: 0022
Revises:     0021
Create Date: 2026-08-10
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
    """Create the additive client-project-day rollup table."""
    op.create_table(
        "client_project_rollup",
        sa.Column(
            "client_id",
            sa.Uuid(),
            sa.ForeignKey("opencode_clients.id"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("project_id", sa.String(), primary_key=True, nullable=False),
        sa.Column("day", sa.Date(), primary_key=True, nullable=False),
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
            "estimated_cost_usd",
            sa.Numeric(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    # Client-scoped day-range scans of the hybrid read path; the composite
    # primary key already covers the per-(client, project, day) lookups.
    op.create_index(
        "ix_client_project_rollup_client_day",
        "client_project_rollup",
        ["client_id", "day"],
    )


def downgrade() -> None:
    """Drop the client_project_rollup table."""
    op.drop_index(
        "ix_client_project_rollup_client_day",
        table_name="client_project_rollup",
    )
    op.drop_table("client_project_rollup")
