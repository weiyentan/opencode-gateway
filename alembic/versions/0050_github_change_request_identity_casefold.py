"""Enforce case-insensitive GitHub change-request ownership.

GitHub repository owner and repository path components are case-insensitive.
The existing migration 0039 unique index compares the raw repository string,
so historical mixed-case values could let two AFK runs claim one PR. This
migration preflights canonical collisions and malformed identities, aborting
with affected run IDs when history is ambiguous. Otherwise it adds a partial
unique expression index over the canonical GitHub identity. Existing strings
are preserved so application rollback does not depend on restoring casing.
GitLab rows remain untouched.

Revision ID: 0050
Revises:     0049
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0050"
down_revision: Union[str, None] = "0049"  # noqa: UP007
branch_labels: Union[str, Sequence[str], None] = None  # noqa: UP007
depends_on: Union[str, Sequence[str], None] = None  # noqa: UP007

_CANONICAL_REPOSITORY = """
lower(regexp_replace(
    regexp_replace(rtrim(change_request_repository, '/'), '(\\.git)+$', '', 'i'),
    '^([^/]+):(80|443)/', '\\1/', 'i'
))
"""


def upgrade() -> None:
    """Refuse ambiguous history, then enforce GitHub case-fold uniqueness."""
    op.execute(
        f"""
        DO $migration$
        DECLARE collision_summary text;
        DECLARE malformed_summary text;
        BEGIN
            SELECT string_agg(
                format('AFK run %s has malformed repository identity %s',
                       afk_run_id, change_request_repository), '; '
            )
            INTO malformed_summary
            FROM (
                SELECT afk_run_id, change_request_repository
                FROM afk_runs
                WHERE change_request_provider = 'github'
                  AND change_request_repository IS NOT NULL
                  AND ({_CANONICAL_REPOSITORY}) !~ '^[^/]+/[^/]+/[^/]+$'
                ORDER BY afk_run_id
                LIMIT 20
            ) AS malformed;

            IF malformed_summary IS NOT NULL THEN
                RAISE EXCEPTION
                    'Cannot install GitHub case-folded change-request ownership index. '
                    'Resolve malformed historical repository identities manually; '
                    'no records were changed. Details: %', malformed_summary;
            END IF;

            SELECT string_agg(
                format('%s #%s -> AFK runs [%s]', repository_key,
                       change_request_external_id, run_ids), '; '
            )
            INTO collision_summary
            FROM (
                SELECT ({_CANONICAL_REPOSITORY}) AS repository_key,
                       change_request_external_id,
                       string_agg(afk_run_id, ', ' ORDER BY afk_run_id) AS run_ids
                FROM afk_runs
                WHERE change_request_provider = 'github'
                  AND change_request_repository IS NOT NULL
                  AND change_request_external_id IS NOT NULL
                GROUP BY ({_CANONICAL_REPOSITORY}), change_request_external_id
                HAVING count(*) > 1
                ORDER BY ({_CANONICAL_REPOSITORY}), change_request_external_id
                LIMIT 20
            ) AS collisions;

            IF collision_summary IS NOT NULL THEN
                RAISE EXCEPTION
                    'Cannot install GitHub case-folded change-request ownership index. '
                    'Resolve historical identity collisions manually; no records were '
                    'changed. Collisions: %', collision_summary;
            END IF;
        END
        $migration$;
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_afk_runs_github_change_request_identity_casefold
        ON afk_runs (
            lower(regexp_replace(
                regexp_replace(rtrim(change_request_repository, '/'), '(\\.git)+$', '', 'i'),
                '^([^/]+):(80|443)/', '\\1/', 'i'
            )),
            change_request_external_id
        )
        WHERE change_request_provider = 'github'
          AND change_request_repository IS NOT NULL
          AND change_request_external_id IS NOT NULL
        """
    )


def downgrade() -> None:
    """Drop only the additive GitHub case-fold index."""
    op.drop_index(
        "uq_afk_runs_github_change_request_identity_casefold",
        table_name="afk_runs",
    )
