"""Nullable change-request identity for two-phase execution bindings (issue #590).

The two-phase lifecycle provisions a ``running`` execution binding at AWX
start (attached to a pre-provisioned ``afk_run_id``) and updates the same
row to a terminal outcome at completion.  Failed or cancelled executions
must persist without a change request or a resolved session, so the four
provider-resource-identity columns become nullable:

* ``provider``, ``repository_url``, ``entity_type``, ``entity_number`` —
  currently NOT NULL (migration 0037) — drop to NULL so a resource-less
  binding can be stored.  ``external_session_id`` was already nullable.
* ``outcome`` already accepts ``running`` (plain string column, no check
  constraint) — the domain enum gained the member without schema change.

The migration is additive-only: no existing row is affected, no data is
rewritten, and no constraint beyond nullability changes.

Downgrade restores the 0037/0040 NOT NULL contract after backfilling any
resource-less rows with placeholders so the constraints can be re-added on
a database that has stored such rows (placeholder values are never
produced by the two-phase write path — they exist only to make the
downgrade mechanical).

Revision ID: 0041
Revises:     0040
Create Date: 2026-08-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0041"
down_revision: Union[str, None] = "0040"  # noqa: UP007
branch_labels: Union[str, Sequence[str], None] = None  # noqa: UP007
depends_on: Union[str, Sequence[str], None] = None  # noqa: UP007

_RESOURCE_COLUMNS = ("provider", "repository_url", "entity_type", "entity_number")


def upgrade() -> None:
    """Drop NOT NULL on the change-request identity columns (additive)."""
    for column in _RESOURCE_COLUMNS:
        op.alter_column("execution_bindings", column, existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    """Restore NOT NULL after backfilling resource-less rows with placeholders."""
    op.execute(
        "UPDATE execution_bindings SET "
        "provider = COALESCE(provider, ''), "
        "repository_url = COALESCE(repository_url, ''), "
        "entity_type = COALESCE(entity_type, ''), "
        "entity_number = COALESCE(entity_number, '') "
        "WHERE provider IS NULL OR repository_url IS NULL "
        "OR entity_type IS NULL OR entity_number IS NULL"
    )
    for column in _RESOURCE_COLUMNS:
        op.alter_column("execution_bindings", column, existing_type=sa.String(), nullable=False)
