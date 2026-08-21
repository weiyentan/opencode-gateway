"""Add the execution-binding table (issue #545).

Creates one additive table — ``execution_bindings`` — without touching any
existing table.  This table is the DB foundation for explicit AWX execution
bindings: one row per AWX job, linking it to an OpenCode external session
and a normalized provider resource identity.

Write-semantics contract (enforced by the repository, guaranteed by the
constraints below):

* **Idempotent by AWX job identity** — keyed by
  ``UNIQUE (awx_job_id)`` so repeating the same binding is a no-op, and a
  new AWX job for the same resource creates a separate binding.
* **Provider resource identity is NOT unique** — allowing multiple failed
  and successful executions for the same change request.
* **Bounded failure metadata** — ``failure_reason`` (short label) and
  ``failure_summary`` (truncated text) carry bounded diagnostic information.
  Raw ``extra_vars``, stdout, prompts, tokens, or arbitrary AWX payloads
  are never stored.

Downgrade drops the table and its indexes.

Revision ID: 0037
Revises:     0036
Create Date: 2026-08-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0037"
down_revision: Union[str, None] = "0036"  # noqa: UP007
branch_labels: Union[str, Sequence[str], None] = None  # noqa: UP007
depends_on: Union[str, Sequence[str], None] = None  # noqa: UP007


def upgrade() -> None:
    """Create the execution-binding table (additive)."""
    op.create_table(
        "execution_bindings",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # AWX job identity — unique, one row per AWX job.
        sa.Column("awx_job_id", sa.BigInteger(), nullable=False),
        # AWX job template identity — the numeric template that launched the
        # job (carried by AWXJobIdentity and persisted, never hardcoded).
        sa.Column("job_template_id", sa.BigInteger(), nullable=False),
        # OpenCode external session ID — nullable (may not be resolved yet).
        sa.Column("external_session_id", sa.String(), nullable=True),
        # Normalized provider resource identity — NOT unique (multiple
        # executions per resource are allowed).
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("repository_url", sa.String(), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("entity_number", sa.String(), nullable=False),
        # Terminal outcome — nullable (set when the execution completes).
        sa.Column("outcome", sa.String(), nullable=True),
        # Optional source event ID (the originating EDA event).
        sa.Column("source_event_id", sa.String(), nullable=True),
        # Optional branch/title metadata — bounded, nullable.
        sa.Column("branch", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        # Bounded failure metadata — nullable, short labels + truncated text.
        sa.Column("failure_reason", sa.String(), nullable=True),
        sa.Column("failure_summary", sa.String(), nullable=True),
        # Timestamps.
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
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
        # AWX job uniqueness constraint — one row per AWX job.
        sa.UniqueConstraint(
            "awx_job_id",
            name="uq_execution_bindings_awx_job_id",
        ),
    )
    # Index on provider resource identity for query by resource.
    op.create_index(
        "ix_execution_bindings_resource",
        "execution_bindings",
        ["provider", "repository_url", "entity_type", "entity_number"],
    )
    # Index on external session ID for query by session.
    op.create_index(
        "ix_execution_bindings_session",
        "execution_bindings",
        ["external_session_id"],
    )


def downgrade() -> None:
    """Drop the execution-binding table."""
    op.drop_table("execution_bindings")
