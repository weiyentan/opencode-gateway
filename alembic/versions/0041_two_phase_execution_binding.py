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

Downgrade restores the 0037/0040 NOT NULL contract, but refuses to proceed
if resource-less rows exist: backfilling them with empty strings would
manufacture invalid identities (empty strings are not valid enum values),
so the operator must delete or backfill such rows manually before
downgrading.

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
    """Refuse to downgrade if resource-less two-phase rows exist.

    The two-phase lifecycle (issue #590) allows ``execution_bindings``
    rows with NULL resource-identity columns (``provider``,
    ``repository_url``, ``entity_type``, ``entity_number``).  A
    downgrade that backfills these with empty strings would manufacture
    invalid identities (empty strings are not valid enum values), so
    the downgrade refuses to proceed when such rows exist.

    The operator must either:
    * Delete resource-less rows before downgrading, or
    * Supply valid resource identities for each affected row.
    """
    try:
        conn = op.get_bind()
        result = conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM execution_bindings "
                "WHERE provider IS NULL OR repository_url IS NULL "
                "OR entity_type IS NULL OR entity_number IS NULL"
            )
        )
        count = result.scalar()
        if count > 0:
            raise RuntimeError(
                f"Cannot downgrade: {count} execution_binding(s) have NULL "
                f"resource-identity columns.  Delete or backfill these rows "
                f"before downgrading, or use a manual migration."
            )
    except AttributeError:
        # Offline SQL render mode (alembic command with sql=True): the
        # fake connection returns a result without .scalar(), so the
        # guard cannot run.  The offline render only produces ALTER
        # COLUMN statements — the runtime guard is exercised by the
        # direct downgrade tests (test_downgrade_refuses_when_null_rows_exist
        # and test_downgrade_succeeds_when_no_null_rows).
        pass
    for column in _RESOURCE_COLUMNS:
        op.alter_column("execution_bindings", column, existing_type=sa.String(), nullable=False)
