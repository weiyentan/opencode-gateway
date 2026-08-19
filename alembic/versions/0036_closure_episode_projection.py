"""Add the closure-episode projection tables (issue #524).

Creates three additive tables — ``closure_links``, ``closure_episodes``,
``closure_unresolved`` — without touching any existing table.  Together they
form the DB-local, versioned, rebuildable closure-episode projection that
derives the change-request->issue closure relationship from the immutable
``engineering_events`` facts (Slice 2).  The projection is never a separate
source of truth and never authoritative over facts.

* **``closure_links``** — the derived current state of one
  change-request->issue link.  Keyed by both endpoint identities (the
  change-request tuple and the issue tuple — flattened stable resource
  identities, no ``engineering_resources`` registry) plus the relationship
  kind (``references`` vs ``declares_closure``, never conflated).
  ``state`` is ``active`` / ``revoked`` (explicit snapshot-diff
  revocation) / ``parked`` (conflicting same-timestamp snapshots, never
  arbitrarily won).  Corrected toward the latest derivation on every
  recompute (conflict-update) — the projection is rebuildable from facts.
* **``closure_episodes``** — immutable open->close intervals keyed by the
  issue endpoint identity plus the close observation time.  ``status``
  carries the fixed episode vocabulary (``pending``, ``awaiting_closure``,
  ``unmatched``, ``ambiguous``, ``inferred``, ``superseded`` — unknowns
  never collapsed into one opaque ``unresolved``); the attributed
  change-request tuple is set only for ``inferred`` episodes.  The partial
  unique index ``uq_closure_episodes_current_issue`` guarantees at most one
  current (not-yet-superseded) episode per issue; a reopen/reclose cycle
  marks the earlier episode ``superseded`` (never deleted).
* **``closure_unresolved``** — versioned unresolved records per closed
  episode outcome (``unmatched`` / ``ambiguous``), keyed by
  ``(issue identity, closed_at, reason)``.  ``candidates`` holds the
  competing change-request identities (empty for unmatched).  Never
  tie-broken, never scored; historical records are retained.

Write-semantics contract: the projection is written only by
``afk_outcomes.repository`` ``AsyncpgOutcomeRepository``
(``recompute_closure_projection`` — the event-triggered, DB-local recompute
that runs after the facts transaction commits, best-effort).  Facts-first
write boundary; a failed recompute never blocks ingestion and the
projection converges on the next trigger.  Every row carries
``resolver_version`` (the ``CLOSURE_RESOLVER_VERSION``) and ``derived_at``
(freshness, exposed by Slice 4).

Downgrade drops the three tables in reverse dependency order (no foreign
keys between them; reverse order is defensive for future constraints).

Revision ID: 0036
Revises:     0035
Create Date: 2026-08-19
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0036"
down_revision: Union[str, None] = "0035"  # noqa: UP007
branch_labels: Union[str, Sequence[str], None] = None  # noqa: UP007
depends_on: Union[str, Sequence[str], None] = None  # noqa: UP007


def upgrade() -> None:
    """Create the closure-episode projection tables (additive)."""
    op.create_table(
        "closure_links",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("change_request_provider", sa.String(), nullable=False),
        sa.Column("change_request_repository", sa.String(), nullable=False),
        sa.Column("change_request_external_id", sa.String(), nullable=False),
        sa.Column("issue_provider", sa.String(), nullable=False),
        sa.Column("issue_repository", sa.String(), nullable=False),
        sa.Column("issue_external_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolver_version", sa.String(), nullable=True),
        sa.Column(
            "derived_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
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
            "change_request_provider",
            "change_request_repository",
            "change_request_external_id",
            "issue_provider",
            "issue_repository",
            "issue_external_id",
            "kind",
            name="uq_closure_links_identity",
        ),
    )
    op.create_index(
        "ix_closure_links_issue",
        "closure_links",
        ["issue_provider", "issue_repository", "issue_external_id"],
    )
    op.create_index(
        "ix_closure_links_change_request",
        "closure_links",
        [
            "change_request_provider",
            "change_request_repository",
            "change_request_external_id",
        ],
    )

    op.create_table(
        "closure_episodes",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("issue_provider", sa.String(), nullable=False),
        sa.Column("issue_repository", sa.String(), nullable=False),
        sa.Column("issue_external_id", sa.String(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("change_request_provider", sa.String(), nullable=True),
        sa.Column("change_request_repository", sa.String(), nullable=True),
        sa.Column("change_request_external_id", sa.String(), nullable=True),
        sa.Column("resolver_version", sa.String(), nullable=True),
        sa.Column(
            "derived_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
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
    )
    # At most one current (not-yet-superseded) episode per issue — the
    # current projection pointer.  ``closed_at IS NULL`` rows are open
    # intervals; the partial predicate keys on the superseded marker only.
    op.create_index(
        "uq_closure_episodes_current_issue",
        "closure_episodes",
        ["issue_provider", "issue_repository", "issue_external_id"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )
    op.create_index(
        "ix_closure_episodes_issue",
        "closure_episodes",
        ["issue_provider", "issue_repository", "issue_external_id"],
    )

    op.create_table(
        "closure_unresolved",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("issue_provider", sa.String(), nullable=False),
        sa.Column("issue_repository", sa.String(), nullable=False),
        sa.Column("issue_external_id", sa.String(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column(
            "candidates",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("resolver_version", sa.String(), nullable=True),
        sa.Column(
            "derived_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
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
            "issue_provider",
            "issue_repository",
            "issue_external_id",
            "closed_at",
            "reason",
            name="uq_closure_unresolved_episode_reason",
        ),
    )


def downgrade() -> None:
    """Drop the closure-episode projection tables."""
    op.drop_table("closure_unresolved")
    op.drop_table("closure_episodes")
    op.drop_table("closure_links")
