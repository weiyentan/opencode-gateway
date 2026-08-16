"""Add the exact resource<->session association table (issue #481).

Creates one additive table — ``resource_session_associations`` — without
touching any existing table (``afk_runs``, ``afk_run_sessions``,
``afk_run_entities``, ``engineering_events``, ``delivery_log``,
``unresolved_correlations``, the reporting tables, the usage tables, etc.).

This table backs the exact, deterministic many-to-many resource<->session
association capability of the AFK Outcome Observability read-model.  The
domain package ``afk_outcomes`` supplies the neutral
:class:`~afk_outcomes.models.ResourceSessionAssociation` model and the
``derive_exact_associations`` resolver; this migration is its persistent
form, and ``afk_outcomes.repository`` ``AsyncpgOutcomeRepository`` is the
only writer.

Associations are derived ONLY from explicit stable resource references
carried in session metadata (``provider``, ``repository``, ``resource_type``,
``resource_number``) — never from temporal or heuristic inference.  Every
association records its source reference (which session field carried the
link) so the link is provable and reproducible.

Write-semantics contract (enforced by the repository, guaranteed by the
constraints below):

* **Idempotent link creation** — keyed by
  ``UNIQUE (provider, repository, resource_type, resource_number,
  external_session_id)`` and written with
  ``ON CONFLICT (...) DO UPDATE SET last_seen_at = now()`` so the same
  explicit reference converging on the same association never duplicates a
  row, while ``last_seen_at`` tracks recency of observation.  There is no
  read-modify-write and therefore no advisory lock (a ``DO UPDATE SET`` is
  still a single atomic statement).
* **Session anchor** — ``external_session_id`` is ``NOT NULL`` (the
  deterministic session identity); ``session_id`` (internal Gateway session
  UUID) is a nullable enrichment carrying no FK, mirroring
  ``afk_run_sessions``, so this schema never couples to the exact shape of
  the ``sessions`` table.
* **No completion/finished claim** — the table carries no status/outcome
  columns (PRD Implementation Decision 13).

Revision ID: 0034
Revises:     0033
Create Date: 2026-08-16
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the resource<->session association table (additive)."""
    op.create_table(
        "resource_session_associations",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("external_session_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("repository", sa.String(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_number", sa.String(), nullable=False),
        sa.Column(
            "source_reference",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("resolver_version", sa.String(), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "provider",
            "repository",
            "resource_type",
            "resource_number",
            "external_session_id",
            name="uq_resource_session_associations_resource_session",
        ),
    )
    op.create_index(
        "ix_resource_session_associations_session",
        "resource_session_associations",
        ["external_session_id"],
    )


def downgrade() -> None:
    """Drop the resource<->session association table."""
    op.drop_table("resource_session_associations")
