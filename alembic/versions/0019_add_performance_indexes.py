"""Add indexes for the Aurora Glass dashboard read paths.

The indexes target the date-filtered usage-record endpoints, session and
agent-run listings, child-run lookup, and projection-table joins.  They are
created concurrently so applying the migration does not block normal reads
or writes in PostgreSQL.

Revision ID: 0019
Revises:     0016
Create Date: 2026-08-07
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "ix_opencode_usage_records_reported_at",
        "opencode_usage_records",
        ("reported_at",),
    ),
    (
        "ix_opencode_usage_records_client_reported_at",
        "opencode_usage_records",
        ("client_id", "reported_at"),
    ),
    (
        "ix_opencode_usage_records_session_reported_at",
        "opencode_usage_records",
        ("session_id", "reported_at"),
    ),
    (
        "ix_sessions_client_last_message_at",
        "sessions",
        ("client_id", "last_message_at"),
    ),
    (
        "ix_sessions_parent_last_message_at",
        "sessions",
        ("parent_session_id", "last_message_at"),
    ),
    (
        "ix_opencode_session_contexts_session_id",
        "opencode_session_contexts",
        ("session_id",),
    ),
    (
        "ix_opencode_session_todos_source_session_position",
        "opencode_session_todos",
        ("source_database_id", "external_session_id", "position"),
    ),
    (
        "ix_opencode_source_projects_source_project",
        "opencode_source_projects",
        ("source_database_id", "external_project_id"),
    ),
)


def upgrade() -> None:
    """Create dashboard read-path indexes without blocking PostgreSQL traffic."""
    for name, table, columns in _INDEXES:
        with op.get_context().autocommit_block():
            op.create_index(
                name,
                table,
                list(columns),
                postgresql_concurrently=True,
            )


def downgrade() -> None:
    """Drop dashboard read-path indexes in reverse creation order."""
    for name, table, _columns in reversed(_INDEXES):
        with op.get_context().autocommit_block():
            op.drop_index(name, table_name=table, postgresql_concurrently=True)
