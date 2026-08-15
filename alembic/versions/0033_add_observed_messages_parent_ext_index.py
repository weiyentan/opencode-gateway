"""Add the timeline parent-session lookup index on ``observed_messages``.

The unified execution-transcript timeline (``GET
/api/v1/execution/sessions/{session_id}/timeline``, ADR 0016) reconstructs a
run by walking parent/child session edges.  Its ``edges`` CTE has two
sources: the ``opencode_session_contexts`` linkage table, and a fallback
branch over ``observed_messages`` that joins each observed message's
``parent_external_session_id`` to the ``sessions`` table to recover
parent/child relationships the session context never recorded (``app/api/
execution.py``):

    SELECT m.session_id AS child_id, s2.id AS parent_id
      FROM observed_messages m
      JOIN sessions s2
        ON s2.external_session_id = m.parent_external_session_id
       AND s2.source_database_id = m.source_database_id
     WHERE m.parent_external_session_id IS NOT NULL AND m.session_id IS NOT NULL

Migration 0029 indexed ``observed_messages`` on
``(session_id, source_created_at)``, ``(agent)``, and
``(role, source_created_at)`` — none of which can serve the fallback's
join/filter on ``parent_external_session_id``.  Every timeline request that
exercises the fallback therefore sequence-scans ``observed_messages``.

0033 adds a partial btree index on
``(parent_external_session_id, session_id)``.  It is partial (``WHERE
parent_external_session_id IS NOT NULL AND session_id IS NOT NULL``)
because the fallback branch only touches rows where both columns are
populated: rows with a NULL ``parent_external_session_id`` have no parent
edge to recover, and rows with a NULL ``session_id`` cannot resolve to a
child session.  Excluding those rows keeps the index smaller and cheaper to
maintain than a full-column index.  The index is created CONCURRENTLY
(mirroring 0024/0025) because ``observed_messages`` is written at ingest
time on a hot production path — a blocking index build would stall message
ingest for the duration of the scan.

Revision ID: 0033
Revises:     0032
Create Date: 2026-08-16
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0033"
down_revision: Union[str, None] = "0032"  # noqa: UP007
branch_labels: Union[str, Sequence[str], None] = None  # noqa: UP007
depends_on: Union[str, Sequence[str], None] = None  # noqa: UP007


def upgrade() -> None:
    """Create the partial parent-session lookup index without blocking writes."""
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_observed_messages_parent_ext",
            "observed_messages",
            ["parent_external_session_id", "session_id"],
            postgresql_where=sa.text(
                "parent_external_session_id IS NOT NULL AND session_id IS NOT NULL"
            ),
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    """Drop the partial parent-session lookup index without blocking writes."""
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_observed_messages_parent_ext",
            table_name="observed_messages",
            postgresql_where=sa.text(
                "parent_external_session_id IS NOT NULL AND session_id IS NOT NULL"
            ),
            postgresql_concurrently=True,
        )
