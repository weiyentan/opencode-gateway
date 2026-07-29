"""Backfill opencode_source_projects.name from worktree basename for rows
where name is NULL.

Existing projects ingested before the collector sent the ``name`` field have
a GUID in ``external_project_id`` and a valid ``worktree`` path but no
friendly ``name``.  This migration populates ``name`` by extracting the
directory basename from ``worktree`` so that ``_PROJECT_LABEL_SQL`` can
resolve a human-readable label via the COALESCE chain.

Revision ID: 0018
Revises:     0017
Create Date: 2026-07-30
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: Union[str, None] = "0017"  # noqa: UP007
branch_labels: Union[str, Sequence[str], None] = None  # noqa: UP007
depends_on: Union[str, Sequence[str], None] = None  # noqa: UP007


def upgrade() -> None:
    """Backfill ``name`` from worktree basename for rows missing a
    friendly name but holding a valid worktree path."""
    op.execute(
        sa.text(
            """
            UPDATE opencode_source_projects
            SET name = regexp_replace(worktree, '^.*/', '')
            WHERE name IS NULL
              AND worktree IS NOT NULL
              AND worktree != ''
              AND worktree != '/'
            """
        )
    )


def downgrade() -> None:
    """Revert the backfill — set ``name`` back to NULL for rows whose
    ``name`` matches the basename extracted from ``worktree`` (i.e. rows
    that were populated by this migration's upgrade step)."""
    op.execute(
        sa.text(
            """
            UPDATE opencode_source_projects
            SET name = NULL
            WHERE name IS NOT NULL
              AND worktree IS NOT NULL
              AND worktree != ''
              AND worktree != '/'
              AND name = regexp_replace(worktree, '^.*/', '')
            """
        )
    )
