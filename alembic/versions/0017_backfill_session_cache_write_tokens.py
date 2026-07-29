"""Backfill total_cache_write_tokens, total_cache_read_tokens, and
total_cached_tokens on sessions from opencode_usage_records.

Existing records ingested before migration 0016 added the per-category
cache token columns have non-zero cache_write_tokens in raw records but
zero in session rollups.  This migration recomputes all three cache-token
totals from the raw records so newly-created sessions (or replayed
ingestion) also receive the correct totals.

Revision ID: 0017
Revises:     0016
Create Date: 2026-07-29
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Backfill session cache-token totals from raw usage records."""
    op.execute(
        sa.text(
            """
            UPDATE sessions s
            SET
              total_cache_write_tokens = COALESCE(sub.cw, 0),
              total_cache_read_tokens  = COALESCE(sub.cr, 0),
              total_cached_tokens      = COALESCE(sub.ct, 0)
            FROM (
              SELECT session_id,
                     SUM(cache_write_tokens) AS cw,
                     SUM(cache_read_tokens)  AS cr,
                     SUM(cached_tokens)      AS ct
              FROM opencode_usage_records
              GROUP BY session_id
            ) sub
            WHERE s.id = sub.session_id
            """
        )
    )


def downgrade() -> None:
    """No structural changes to reverse — this migration is data-only.
    A downgrade would require restoring from a pre-migration backup or
    re-ingesting affected records.
    """
