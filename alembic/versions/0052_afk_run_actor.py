"""Add the original AFK trigger actor to ``afk_runs`` (issue #859).

The actor is the provider-qualified username of the account that originally
triggered the AFK run (``github:login`` or ``gitlab:username``).  It is
captured by the producing system (FastAPI EDA Gateway) and bound **once** to
the canonical ``afk_run_id`` at provisioning time (the existing
``POST /api/v1/afk/executions/runs`` authority).  It records who asked for
the work — never a credential, token, raw payload, or arbitrary provider
object.

Immutability and conflict/idempotency behavior mirror the logical execution
target (migration 0051, issue #856), which this column sits beside:

* the provisioning authority is the only writer;
* an identical replay (same actor) returns the same run and actor;
* a replay carrying a different actor for the same provisioning key — or
  omitting an actor against a stored one — is a conflict (409) with no
  reassignment and no erasure;
* later lifecycle operations (change-request binding, execution bindings,
  recoveries, review/developer events, AWX service identities) never update
  the column.

This migration is strictly additive: ``actor`` is nullable with no server
default, so every historic ``afk_runs`` row (and every run provisioned by a
client that does not send an actor) reads back as ``NULL`` — no historic row
is rewritten and no actor is fabricated.

Downgrade drops the nullable column, restoring the migration 0051 shape;
historic rows are untouched.

Validation layering mirrors migration 0051: the actor is deliberately NOT
guarded by a database CHECK constraint.  The column has exactly one writer —
the provisioning authority ``provision_afk_run`` — reachable only through
``POST /api/v1/afk/executions/runs``, whose Pydantic request model
(``AFKRunProvisionRequest.actor``) enforces the anchored provider-qualified
shape (``^(github|gitlab):[A-Za-z0-9_.-]+$``, 3-255 chars) before the
repository layer ever sees the value.

Revision ID: 0052
Revises:     0051
Create Date: 2026-11-23
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0052"
down_revision: Union[str, None] = "0051"  # noqa: UP007
branch_labels: Union[str, Sequence[str], None] = None  # noqa: UP007
depends_on: Union[str, Sequence[str], None] = None  # noqa: UP007


def upgrade() -> None:
    """Add the nullable original-trigger-actor column (additive-only)."""
    op.add_column(
        "afk_runs",
        sa.Column("actor", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    """Drop the actor column (restores the 0051 shape)."""
    op.drop_column("afk_runs", "actor")
