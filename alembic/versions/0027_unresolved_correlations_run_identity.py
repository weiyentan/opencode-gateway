"""Scope unresolved-correlation identity to the run (PR #458 follow-up 2).

Before 0027, low-confidence entity-level rows were keyed by
UNIQUE (provider, repository, entity_type, external_id, method) and the
upsert rewrote afk_run_id = COALESCE(EXCLUDED.afk_run_id, ...) — so a later
run's correlation of the same entity silently MOVED the row to the newest
run id, misattributing historical evidence and confidence.

0027 makes the run part of the row identity:
UNIQUE (provider, repository, entity_type, external_id, afk_run_id, method).
Every writer (afk_outcomes.repository._upsert_unresolved_correlation and
_upsert_engine_unresolved) always supplies a non-null afk_run_id (both
domain models declare it a required str), so the column is made NOT NULL.
The defensive backfill covers any stray NULL row; for run-level rows
external_id IS the run id, so the backfill is exact there (entity-level
rows never carry NULL per the domain).

Downgrade hazard: after 0027, two rows sharing
(provider, repository, entity_type, external_id, method) with different
afk_run_id are legal; downgrade re-adding the old unique constraint fails
on such data. Acceptable — 0026 is brand-new/unmerged, downgrades are
pre-production only.

Revision ID: 0027
Revises:     0026
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── defensive: attribute any stray NULL row before NOT NULL ──────────
    op.execute(
        "UPDATE unresolved_correlations SET afk_run_id = external_id "
        "WHERE afk_run_id IS NULL"
    )
    # ── identity: run joins the unique key ───────────────────────────────
    op.drop_constraint(
        "uq_unresolved_correlations_entity_method",
        "unresolved_correlations",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_unresolved_correlations_entity_run_method",
        "unresolved_correlations",
        ["provider", "repository", "entity_type", "external_id",
         "afk_run_id", "method"],
    )
    # ── nullability — non-null: every writer supplies a run id ───────────
    op.alter_column(
        "unresolved_correlations",
        "afk_run_id",
        existing_type=sa.String(26),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "unresolved_correlations",
        "afk_run_id",
        existing_type=sa.String(26),
        nullable=True,
    )
    op.drop_constraint(
        "uq_unresolved_correlations_entity_run_method",
        "unresolved_correlations",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_unresolved_correlations_entity_method",
        "unresolved_correlations",
        ["provider", "repository", "entity_type", "external_id", "method"],
    )
