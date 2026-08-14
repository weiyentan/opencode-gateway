"""Add owning-change-request lineage provenance to afk_run_entities (issue #456).

Before 0028, commits and review events carried on the owning change
request's branch were persisted as entity-link rows with no link-level
provenance beyond ``role`` / ``correlation_method`` / ``correlation_confidence``
— there was no way to record that a commit/review link was inherited from the
owning change request rather than established by a direct correlation rule.

0028 adds two columns to ``afk_run_entities`` (strictly additive, no backfill):

* ``owning_change_request_id`` — nullable String carrying the external id of
  the change request that owns the entity's branch (set at fetch time for
  commits and reviews; NULL for other entity types and for direct links).
* ``correlation_source`` — String NOT NULL with server default ``'direct'``,
  taking the values ``direct`` (a correlation rule established the link) or
  ``owning_change_request`` (the link was inherited via the owning change
  request's branch lineage).

Both columns are nullable/default-safe: existing rows remain valid with
``owning_change_request_id IS NULL`` and ``correlation_source = 'direct'``
(populated by the server default).  The ORM model
``app/db/models/afk.py:AFKRunEntityLink`` is updated in lockstep.

Revision ID: 0028
Revises:     0027
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "afk_run_entities",
        sa.Column("owning_change_request_id", sa.String(), nullable=True),
    )
    op.add_column(
        "afk_run_entities",
        sa.Column(
            "correlation_source",
            sa.String(),
            nullable=False,
            server_default=sa.text("'direct'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("afk_run_entities", "correlation_source")
    op.drop_column("afk_run_entities", "owning_change_request_id")
