"""opencode_clients and collector_credentials tables.

This migration creates the identity-layer tables for the OpenCode Gateway
observability service.  Future slices will add observability-specific
tables (usage, sessions, etc.).

Revision ID: 0001
Revises:     0000
Create Date: 2025-07-16
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = "0000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create opencode_clients and collector_credentials tables."""

    op.create_table(
        "opencode_clients",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "collector_credentials",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "client_id",
            sa.Uuid(),
            sa.ForeignKey("opencode_clients.id"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("token_prefix", sa.String(8), nullable=False),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index(
        "ix_collector_credentials_client_id",
        "collector_credentials",
        ["client_id"],
    )
    op.create_index(
        "ix_collector_credentials_token_hash",
        "collector_credentials",
        ["token_hash"],
    )


def downgrade() -> None:
    """Remove the identity-layer tables."""
    op.drop_index("ix_collector_credentials_token_hash")
    op.drop_index("ix_collector_credentials_client_id")
    op.drop_table("collector_credentials")
    op.drop_table("opencode_clients")
