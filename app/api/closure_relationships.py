"""Closure-relationships read-only REST API (issue #525).

Three GET endpoints under the versioned namespace answer the operator
questions from PRD #521 by reading the Slice 3 projection tables
(``closure_episodes`` / ``closure_links`` / ``closure_unresolved``,
migration 0036):

- ``GET /issues/current``         — the current issue→change-request answer:
  the current (non-superseded) episode with its status, the single-candidate
  attribution or the unmatched/ambiguous marker, and the evidence links.
- ``GET /issues/episodes``        — the auditable episode/evidence history:
  every immutable episode (``superseded`` included, never hidden), the
  declaration/revocation link states, and the versioned unresolved records.
- ``GET /change-requests/issues`` — reverse lookup: the issues a change
  request references and/or declares closing (paginated).

All responses use the ``{status, data, error}`` envelope and are protected
by the global :class:`~app.core.auth.ApiKeyMiddleware`.  This router is
strictly read-only and makes **no provider API calls** — it reads only the
DB projection/unresolved rows, and never derives a provider-authoritative
causation claim: responses present observed facts plus inferred attribution
with evidence and status.

Freshness (PRD #521 Implementation Decision 16): every projection view
carries the row's ``derived_at`` (last successful recompute) and
``resolver_version``, and the single-issue responses additionally expose a
response-level ``derived_at`` / ``resolver_version``.

The read path follows the ``app/api/afk_outcomes.py`` convention: raw
asyncpg via ``Depends(get_session)``, explicit-column SELECTs, parameterised
filters with 400 on invalid enum/date values, and the
``_db_timeout`` / ``_request_timeout`` helpers.
"""

from __future__ import annotations

import json
from datetime import datetime

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from afk_outcomes.models import (
    CLOSURE_RESOLVER_VERSION,
    ClosureEpisodeStatus,
    ClosureLinkKind,
    Provider,
)
from app.core.config import get_settings
from app.core.metrics import register_closure_projection_metrics
from app.core.schemas.closure_relationships import (
    ClosureCandidateView,
    ClosureEpisodeView,
    ClosureLinkView,
    ClosureUnresolvedView,
    IssueClosureAnswer,
    IssueEpisodeHistory,
)
from app.core.schemas.usage import PaginatedResponse
from app.core.telemetry import timed_operation
from app.core.timeouts import db_timeout as _db_timeout
from app.core.timeouts import request_timeout as _request_timeout
from app.db.session import get_session

router = APIRouter(tags=["closure-relationships"])

# Eagerly register the projection-recompute metrics on the process-wide
# registry so the metrics snapshot seam always surfaces them (zero-valued
# until a recompute owner records a failure/success) — see
# :func:`app.core.metrics.register_closure_projection_metrics`.
register_closure_projection_metrics()

# ── Valid enum filter values (locked domain vocabulary) ──────────────────────

_VALID_PROVIDERS = frozenset(m.value for m in Provider)
_VALID_STATUS = frozenset(m.value for m in ClosureEpisodeStatus)
_VALID_KIND = frozenset(m.value for m in ClosureLinkKind)

# ── Shared column lists ──────────────────────────────────────────────────────

_EPISODE_COLUMNS = """
    issue_provider, issue_repository, issue_external_id,
    opened_at, closed_at, status,
    change_request_provider, change_request_repository, change_request_external_id,
    resolver_version, derived_at, superseded_at
"""

_LINK_COLUMNS = """
    change_request_provider, change_request_repository, change_request_external_id,
    issue_provider, issue_repository, issue_external_id,
    kind, state, revoked_at, resolver_version, derived_at
"""

_UNRESOLVED_COLUMNS = """
    issue_provider, issue_repository, issue_external_id,
    closed_at, reason, candidates, resolver_version, derived_at
"""


# ── Helpers ──────────────────────────────────────────────────────────────────


def _parse_datetime(raw: str | None, param_name: str) -> datetime | None:
    """Parse an ISO-8601 datetime query param, raising 400 on malformed values.

    Handles the ``Z`` suffix (Python 3.9's ``datetime.fromisoformat`` does not)
    by normalising it to ``+00:00``.
    """
    if raw is None:
        return None
    normalized = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {param_name}: {raw!r} is not a valid ISO-8601 datetime",
        ) from None


def _require_enum_value(raw: str | None, valid: frozenset[str], param_name: str) -> None:
    """Raise 400 when *raw* is not a member of *valid*."""
    if raw is not None and raw not in valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid {param_name}: {raw!r}. "
                f"Valid values: {', '.join(sorted(valid))}"
            ),
        )


def _validate_window(
    start: datetime | None, end: datetime | None, param_name: str
) -> None:
    """Raise 400 when an (inverted) window start is after its end."""
    if start is not None and end is not None and start > end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{param_name} must not be after {param_name.replace('from', 'to')}",
        )


def _decode_candidates(raw: object) -> list[ClosureCandidateView]:
    """Decode the JSONB ``candidates`` column into candidate views.

    asyncpg returns JSONB columns as JSON-encoded ``str`` (no type codec is
    registered — see ``app/api/reporting.py``); unit-test mock rows provide
    already-decoded lists.  Both shapes are accepted.
    """
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, list):
        return []
    return [ClosureCandidateView.model_validate(item) for item in raw]


def _max_derived_at(*values: datetime | None) -> datetime | None:
    """The latest non-null timestamp among the given values (or ``None``)."""
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _resolver_version_of(*versions: str | None) -> str:
    """The first non-null resolver version, falling back to the current
    ``CLOSURE_RESOLVER_VERSION`` — the version the projector writes today."""
    for version in versions:
        if version is not None:
            return version
    return CLOSURE_RESOLVER_VERSION


# ── Row builders ─────────────────────────────────────────────────────────────


def _episode_view(row: asyncpg.Record) -> ClosureEpisodeView:
    """Build a :class:`ClosureEpisodeView` from a ``closure_episodes`` row."""
    return ClosureEpisodeView(
        issue_provider=row["issue_provider"],
        issue_repository=row["issue_repository"],
        issue_external_id=row["issue_external_id"],
        opened_at=row["opened_at"],
        closed_at=row["closed_at"],
        status=row["status"],
        change_request_provider=row["change_request_provider"],
        change_request_repository=row["change_request_repository"],
        change_request_external_id=row["change_request_external_id"],
        resolver_version=row["resolver_version"],
        derived_at=row["derived_at"],
        superseded_at=row["superseded_at"],
    )


def _link_view(row: asyncpg.Record) -> ClosureLinkView:
    """Build a :class:`ClosureLinkView` from a ``closure_links`` row."""
    return ClosureLinkView(
        change_request_provider=row["change_request_provider"],
        change_request_repository=row["change_request_repository"],
        change_request_external_id=row["change_request_external_id"],
        issue_provider=row["issue_provider"],
        issue_repository=row["issue_repository"],
        issue_external_id=row["issue_external_id"],
        kind=row["kind"],
        state=row["state"],
        revoked_at=row["revoked_at"],
        resolver_version=row["resolver_version"],
        derived_at=row["derived_at"],
    )


def _unresolved_view(row: asyncpg.Record) -> ClosureUnresolvedView:
    """Build a :class:`ClosureUnresolvedView` from a ``closure_unresolved`` row."""
    return ClosureUnresolvedView(
        issue_provider=row["issue_provider"],
        issue_repository=row["issue_repository"],
        issue_external_id=row["issue_external_id"],
        closed_at=row["closed_at"],
        reason=row["reason"],
        candidates=_decode_candidates(row["candidates"]),
        resolver_version=row["resolver_version"],
        derived_at=row["derived_at"],
    )


# ── Query helpers ────────────────────────────────────────────────────────────


async def _fetch_issue_evidence(
    conn: asyncpg.Connection,
    provider: str,
    repository: str,
    external_id: str,
    *,
    db_timeout_seconds: int,
) -> list[ClosureLinkView]:
    """All ``closure_links`` rows for one issue — the declaration/revocation
    evidence (both kinds, states visible)."""
    async with timed_operation("db.query.closure.evidence", "db"):
        async with _db_timeout("db.query.closure.evidence", db_timeout_seconds):
            rows = await conn.fetch(
                f"""
                SELECT {_LINK_COLUMNS}
                FROM closure_links
                WHERE issue_provider = $1 AND issue_repository = $2
                  AND issue_external_id = $3
                ORDER BY kind, change_request_repository, change_request_external_id
                """,
                provider,
                repository,
                external_id,
            )
    return [_link_view(r) for r in rows]


async def _fetch_issue_unresolved(
    conn: asyncpg.Connection,
    provider: str,
    repository: str,
    external_id: str,
    *,
    db_timeout_seconds: int,
) -> list[ClosureUnresolvedView]:
    """All ``closure_unresolved`` rows for one issue (versioned, retained)."""
    async with timed_operation("db.query.closure.unresolved", "db"):
        async with _db_timeout("db.query.closure.unresolved", db_timeout_seconds):
            rows = await conn.fetch(
                f"""
                SELECT {_UNRESOLVED_COLUMNS}
                FROM closure_unresolved
                WHERE issue_provider = $1 AND issue_repository = $2
                  AND issue_external_id = $3
                ORDER BY closed_at, reason
                """,
                provider,
                repository,
                external_id,
            )
    return [_unresolved_view(r) for r in rows]


async def _fetch_current_answer(
    conn: asyncpg.Connection,
    provider: str,
    repository: str,
    external_id: str,
    *,
    db_timeout_seconds: int,
) -> IssueClosureAnswer | None:
    """Compose the current issue→change-request answer (3 queries, no N+1)."""
    async with timed_operation("db.query.closure.answer.episode", "db"):
        async with _db_timeout("db.query.closure.answer.episode", db_timeout_seconds):
            row = await conn.fetchrow(
                f"""
                SELECT {_EPISODE_COLUMNS}
                FROM closure_episodes
                WHERE issue_provider = $1 AND issue_repository = $2
                  AND issue_external_id = $3
                  AND superseded_at IS NULL
                """,
                provider,
                repository,
                external_id,
            )
    if row is None:
        return None
    episode = _episode_view(row)

    evidence = await _fetch_issue_evidence(
        conn, provider, repository, external_id, db_timeout_seconds=db_timeout_seconds
    )
    unresolved = await _fetch_issue_unresolved(
        conn, provider, repository, external_id, db_timeout_seconds=db_timeout_seconds
    )

    # The unresolved record matching the current episode (a closed episode)
    # carries the ambiguous candidates / the unmatched marker.
    candidates: list[ClosureCandidateView] = []
    if episode.closed_at is not None:
        for record in unresolved:
            if record.closed_at == episode.closed_at:
                candidates = record.candidates
                break

    return IssueClosureAnswer(
        issue_provider=provider,
        issue_repository=repository,
        issue_external_id=external_id,
        episode=episode,
        candidates=candidates,
        evidence=evidence,
        derived_at=_max_derived_at(
            episode.derived_at,
            *[link.derived_at for link in evidence],
            *[record.derived_at for record in unresolved],
        ),
        resolver_version=_resolver_version_of(
            episode.resolver_version,
            *[link.resolver_version for link in evidence],
        ),
    )


async def _fetch_episode_history(
    conn: asyncpg.Connection,
    provider: str,
    repository: str,
    external_id: str,
    status_filter: str | None,
    closed_from: datetime | None,
    closed_to: datetime | None,
    *,
    db_timeout_seconds: int,
) -> IssueEpisodeHistory | None:
    """Compose the auditable episode/evidence history (3 queries, no N+1).

    Every immutable episode is returned — ``superseded`` included, never
    hidden.  ``closed_at IS NULL`` (the currently-open interval) sorts last.
    """
    params: list[object] = [provider, repository, external_id]
    filters: list[str] = [
        "issue_provider = $1",
        "issue_repository = $2",
        "issue_external_id = $3",
    ]
    if status_filter is not None:
        filters.append(f"status = ${len(params) + 1}")
        params.append(status_filter)
    if closed_from is not None:
        filters.append(f"closed_at >= ${len(params) + 1}")
        params.append(closed_from)
    if closed_to is not None:
        filters.append(f"closed_at <= ${len(params) + 1}")
        params.append(closed_to)
    where_clause = " AND ".join(filters)

    async with timed_operation("db.query.closure.history.episodes", "db"):
        async with _db_timeout("db.query.closure.history.episodes", db_timeout_seconds):
            episode_rows = await conn.fetch(
                f"""
                SELECT {_EPISODE_COLUMNS}
                FROM closure_episodes
                WHERE {where_clause}
                ORDER BY closed_at NULLS LAST, opened_at NULLS LAST
                """,
                *params,
            )
    if not episode_rows:
        return None

    evidence = await _fetch_issue_evidence(
        conn, provider, repository, external_id, db_timeout_seconds=db_timeout_seconds
    )
    unresolved = await _fetch_issue_unresolved(
        conn, provider, repository, external_id, db_timeout_seconds=db_timeout_seconds
    )
    episodes = [_episode_view(r) for r in episode_rows]

    return IssueEpisodeHistory(
        issue_provider=provider,
        issue_repository=repository,
        issue_external_id=external_id,
        episodes=episodes,
        evidence=evidence,
        unresolved=unresolved,
        derived_at=_max_derived_at(
            *[episode.derived_at for episode in episodes],
            *[link.derived_at for link in evidence],
            *[record.derived_at for record in unresolved],
        ),
        resolver_version=_resolver_version_of(
            *[episode.resolver_version for episode in episodes]
        ),
    )


async def _fetch_reverse_links(
    conn: asyncpg.Connection,
    provider: str,
    repository: str,
    external_id: str,
    kind: str | None,
    limit: int,
    offset: int,
    *,
    db_timeout_seconds: int,
) -> PaginatedResponse[ClosureLinkView]:
    """Execute count + data queries for the reverse change-request→issues lookup."""
    params: list[object] = [provider, repository, external_id]
    filters: list[str] = [
        "change_request_provider = $1",
        "change_request_repository = $2",
        "change_request_external_id = $3",
    ]
    if kind is not None:
        filters.append(f"kind = ${len(params) + 1}")
        params.append(kind)
    where_clause = " AND ".join(filters)

    async with timed_operation("db.query.closure.reverse.count", "db"):
        async with _db_timeout("db.query.closure.reverse.count", db_timeout_seconds):
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM closure_links WHERE {where_clause}", *params
            )

    data_sql = f"""
        SELECT {_LINK_COLUMNS}
        FROM closure_links
        WHERE {where_clause}
        ORDER BY kind, issue_repository, issue_external_id
        LIMIT ${len(params) + 1}
        OFFSET ${len(params) + 2}
    """
    async with timed_operation("db.query.closure.reverse.data", "db"):
        async with _db_timeout("db.query.closure.reverse.data", db_timeout_seconds):
            rows = await conn.fetch(data_sql, *params, limit, offset)

    return PaginatedResponse(
        items=[_link_view(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Endpoints
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/issues/current")
async def get_current_answer(
    request: Request,
    provider: str = Query(...),
    repository: str = Query(...),
    external_id: str = Query(...),
    conn: asyncpg.Connection = Depends(get_session),
) -> IssueClosureAnswer:
    """The current issue→change-request answer, keyed by the issue endpoint
    identity (flattened stable resource identity tuple).

    Returns the current episode with its status, the single-candidate
    attribution or the unmatched/ambiguous marker, and the evidence links —
    observed facts + inferred attribution, never a provider-authoritative
    claim.
    """
    _require_enum_value(provider, _VALID_PROVIDERS, "provider")
    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        answer = await _fetch_current_answer(
            conn,
            provider,
            repository,
            external_id,
            db_timeout_seconds=settings.database_timeout_seconds,
        )
    if answer is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Closure episode not found for issue: "
                f"{provider}:{repository}:{external_id}"
            ),
        )
    return answer


@router.get("/issues/episodes")
async def get_episode_history(
    request: Request,
    provider: str = Query(...),
    repository: str = Query(...),
    external_id: str = Query(...),
    status_filter: str | None = Query(default=None, alias="status"),
    closed_from: str | None = Query(default=None),
    closed_to: str | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_session),
) -> IssueEpisodeHistory:
    """The auditable episode/evidence history for one issue.

    Every immutable episode is returned — including ``superseded`` ones,
    never hidden — with endpoint identities, declaration/revocation
    snapshots, ``resolver_version``, and status.
    """
    _require_enum_value(provider, _VALID_PROVIDERS, "provider")
    _require_enum_value(status_filter, _VALID_STATUS, "status")

    closed_from_dt = _parse_datetime(closed_from, "closed_from")
    closed_to_dt = _parse_datetime(closed_to, "closed_to")
    _validate_window(closed_from_dt, closed_to_dt, "closed_from")

    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        history = await _fetch_episode_history(
            conn,
            provider,
            repository,
            external_id,
            status_filter,
            closed_from_dt,
            closed_to_dt,
            db_timeout_seconds=settings.database_timeout_seconds,
        )
    if history is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Closure episodes not found for issue: "
                f"{provider}:{repository}:{external_id}"
            ),
        )
    return history


@router.get("/change-requests/issues")
async def get_change_request_issues(
    request: Request,
    provider: str = Query(...),
    repository: str = Query(...),
    external_id: str = Query(...),
    kind: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    conn: asyncpg.Connection = Depends(get_session),
) -> PaginatedResponse[ClosureLinkView]:
    """Reverse lookup: the issues a change request references and/or declares
    closing (``closure_links`` rows keyed by the change-request endpoint
    identity), with the derived link state visible per row.
    """
    _require_enum_value(provider, _VALID_PROVIDERS, "provider")
    _require_enum_value(kind, _VALID_KIND, "kind")
    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        return await _fetch_reverse_links(
            conn,
            provider,
            repository,
            external_id,
            kind,
            limit,
            offset,
            db_timeout_seconds=settings.database_timeout_seconds,
        )
