"""AFK outcomes read-only REST API (issue #452).

Five GET endpoints under the versioned namespace expose the AFK outcome
read-model stored by ``afk_outcomes.repository.AsyncpgOutcomeRepository``:

- ``GET /runs``        — list runs, filterable by repository, window
  (started/finished/seen bounds), status, outcome, and origin; paginated.
- ``GET /runs/{id}``   — the full chain for one run (aggregate, outcome,
  engineering entities grouped by type, sessions, agents, usage/cost).
- ``GET /entities``    — engineering entities with their run links, correlation
  provenance, and superseded state.
- ``GET /correlations`` — unresolved correlations with method/confidence/
  evidence/resolver_version and provisional markers.
- ``GET /change-requests`` — one summary row per provider/repository/
  change-request identity (issue #610): provider state derived from observed
  facts, AFK automation state, total estimated cost, latest linked activity,
  and aggregated execution counts; filterable by provider, repository,
  provider state, automation state, and activity window; paginated.

All responses use the existing ``{status, data, error}`` envelope and are
protected by the global :class:`~app.core.auth.ApiKeyMiddleware`.  This router
is strictly read-only — backfill remains CLI-only (#449).

The read path maps 1:1 to stored columns and follows the ``app/api/usage.py``
convention: raw asyncpg via ``Depends(get_session)``, explicit-column SELECTs,
parameterised filters with 400 on invalid enum/date values, and the
``_db_timeout``/``_request_timeout`` helpers.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from afk_outcomes.models import (
    CorrelationEvidence,
    EngineeringOutcome,
    EngineeringOutcomeStatus,
    Provider,
    RunStatus,
    UnresolvedReason,
)
from app.core.config import get_settings
from app.core.schemas.afk import (
    ChangeRequestExecutionCounts,
    ChangeRequestSummaryRow,
    EntityLink,
    EntityRow,
    RunDetail,
    RunSummary,
    SessionLink,
    UnresolvedCorrelationRow,
    UsageAggregate,
)
from app.core.schemas.usage import PaginatedResponse
from app.core.telemetry import timed_operation
from app.core.timeouts import db_timeout as _db_timeout
from app.core.timeouts import request_timeout as _request_timeout
from app.db.session import get_session

router = APIRouter(tags=["afk-outcomes"])

# ── Valid enum filter values (locked domain vocabulary) ──────────────────────

_VALID_STATUS = frozenset(m.value for m in RunStatus)
_VALID_OUTCOME = frozenset(m.value for m in EngineeringOutcomeStatus)
_VALID_ORIGIN = frozenset(m.value for m in Provider)
_VALID_REASON = frozenset(m.value for m in UnresolvedReason)

# Provider state derived from observed change-request facts (issue #610).
# It is a fact-derived display vocabulary, NOT EngineeringOutcomeStatus:
# only merged/closed/open are determinable from event types.
_VALID_PROVIDER_STATE = frozenset({"merged", "closed", "open"})

# AFK automation state is the owning lifecycle's ``afk_runs.status`` — the
# aggregate lifecycle vocabulary from ``resolve_afk_run_status`` (including
# the provisional ``pending``), not the agent-run ``RunStatus`` enum.
_VALID_AUTOMATION_STATE = frozenset(
    {"pending", "running", "completed", "failed", "cancelled"}
)

# Entity-type → detail response field name grouping.
_ENTITY_TYPE_FIELDS = {
    "issue": "issues",
    "change_request": "change_requests",
    "review": "reviews",
    "commit": "commits",
    "merge_event": "merge_events",
}

# A derived link is provisional (inferred) when its role is not a definitive
# "resolved" — i.e. "referenced" (sub-threshold confidence) or "noise".
_RESOLVED_ROLE = "resolved"


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


def _parse_evidence(raw: object) -> list[CorrelationEvidence]:
    """Parse a JSONB evidence array into :class:`CorrelationEvidence` items."""
    if not raw:
        return []
    if not isinstance(raw, list):
        return []
    return [CorrelationEvidence.model_validate(item) for item in raw]


def _build_run_filters(
    repository: str | None,
    started_from: datetime | None,
    started_to: datetime | None,
    finished_from: datetime | None,
    finished_to: datetime | None,
    seen_from: datetime | None,
    seen_to: datetime | None,
    status_filter: str | None,
    outcome_filter: str | None,
    origin_filter: str | None,
) -> tuple[str, list[object]]:
    """Build a WHERE clause and parameter list for the list-runs query."""
    params: list[object] = []
    filters: list[str] = ["TRUE"]

    if repository is not None:
        filters.append(
            "EXISTS (SELECT 1 FROM afk_run_entities re "
            f"WHERE re.afk_run_id = r.afk_run_id AND re.repository = ${len(params) + 1})"
        )
        params.append(repository)

    for column, value, op in (
        ("started_at", started_from, ">="),
        ("started_at", started_to, "<="),
        ("finished_at", finished_from, ">="),
        ("finished_at", finished_to, "<="),
        ("last_seen_at", seen_from, ">="),
        ("last_seen_at", seen_to, "<="),
    ):
        if value is None:
            continue
        filters.append(f"r.{column} {op} ${len(params) + 1}")
        params.append(value)

    if status_filter is not None:
        filters.append(f"r.status = ${len(params) + 1}")
        params.append(status_filter)
    if outcome_filter is not None:
        filters.append(f"r.outcome_status = ${len(params) + 1}")
        params.append(outcome_filter)
    if origin_filter is not None:
        filters.append(f"r.provider = ${len(params) + 1}")
        params.append(origin_filter)

    return " AND ".join(filters), params


def _entity_link(row: asyncpg.Record) -> EntityLink:
    """Build an :class:`EntityLink` from an ``afk_run_entities`` row."""
    role = row["role"]
    return EntityLink(
        entity_id=f"{row['entity_type']}:{row['external_id']}",
        entity_type=row["entity_type"],
        external_id=row["external_id"],
        provider=row["provider"],
        repository=row["repository"],
        role=role,
        correlation_method=row["correlation_method"],
        correlation_confidence=row["correlation_confidence"],
        evidence=_parse_evidence(row["evidence"]),
        resolver_version=row["resolver_version"],
        owning_change_request_id=row["owning_change_request_id"],
        correlation_source=row["correlation_source"],
        provisional=role != _RESOLVED_ROLE,
    )


# ── Query helpers ────────────────────────────────────────────────────────────


async def _fetch_runs(
    conn: asyncpg.Connection,
    repository: str | None,
    started_from: datetime | None,
    started_to: datetime | None,
    finished_from: datetime | None,
    finished_to: datetime | None,
    seen_from: datetime | None,
    seen_to: datetime | None,
    status_filter: str | None,
    outcome_filter: str | None,
    origin_filter: str | None,
    limit: int,
    offset: int,
    *,
    db_timeout_seconds: int,
) -> PaginatedResponse[RunSummary]:
    """Execute count + data queries for the list-runs endpoint."""
    where_clause, params = _build_run_filters(
        repository,
        started_from,
        started_to,
        finished_from,
        finished_to,
        seen_from,
        seen_to,
        status_filter,
        outcome_filter,
        origin_filter,
    )

    count_sql = f"SELECT COUNT(*) FROM afk_runs r WHERE {where_clause}"
    async with timed_operation("db.query.afk.runs.count", "db"):
        async with _db_timeout("db.query.afk.runs.count", db_timeout_seconds):
            total = await conn.fetchval(count_sql, *params)

    data_sql = f"""
        SELECT r.afk_run_id, r.provider, r.status, r.title, r.started_at,
               r.finished_at, r.outcome_status, r.first_seen_at, r.last_seen_at
        FROM afk_runs r
        WHERE {where_clause}
        ORDER BY r.last_seen_at DESC
        LIMIT ${len(params) + 1}
        OFFSET ${len(params) + 2}
    """
    async with timed_operation("db.query.afk.runs.data", "db"):
        async with _db_timeout("db.query.afk.runs.data", db_timeout_seconds):
            rows = await conn.fetch(data_sql, *params, limit, offset)

    items = [
        RunSummary(
            afk_run_id=r["afk_run_id"],
            provider=r["provider"],
            status=r["status"],
            title=r["title"],
            started_at=r["started_at"],
            finished_at=r["finished_at"],
            outcome_status=r["outcome_status"],
            first_seen_at=r["first_seen_at"],
            last_seen_at=r["last_seen_at"],
        )
        for r in rows
    ]
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


async def _fetch_run_detail(
    conn: asyncpg.Connection,
    afk_run_id: str,
    *,
    db_timeout_seconds: int,
) -> RunDetail | None:
    """Compose the full chain for one run (3 queries, no N+1)."""
    async with timed_operation("db.query.afk.run.detail.run", "db"):
        async with _db_timeout("db.query.afk.run.detail.run", db_timeout_seconds):
            run_row = await conn.fetchrow(
                """
                SELECT afk_run_id, provider, status, title, started_at, finished_at,
                       outcome_status, outcome, first_seen_at, last_seen_at
                FROM afk_runs
                WHERE afk_run_id = $1
                """,
                afk_run_id,
            )
    if run_row is None:
        return None

    async with timed_operation("db.query.afk.run.detail.entities", "db"):
        async with _db_timeout("db.query.afk.run.detail.entities", db_timeout_seconds):
            entity_rows = await conn.fetch(
                """
                SELECT provider, repository, entity_type, external_id, role,
                       correlation_method, correlation_confidence, evidence,
                       resolver_version, owning_change_request_id,
                       correlation_source
                FROM afk_run_entities
                WHERE afk_run_id = $1 AND superseded_at IS NULL
                ORDER BY entity_type, external_id
                """,
                afk_run_id,
            )

    async with timed_operation("db.query.afk.run.detail.sessions", "db"):
        async with _db_timeout("db.query.afk.run.detail.sessions", db_timeout_seconds):
            session_rows = await conn.fetch(
                """
                SELECT ars.session_id, ars.external_session_id, ars.started_at,
                       ars.finished_at, s.agent, s.total_input_tokens,
                       s.total_output_tokens, s.total_cache_read_tokens,
                       s.total_cache_write_tokens, s.total_estimated_cost_usd,
                       s.message_count, s.parent_session_id
                FROM afk_run_sessions ars
                LEFT JOIN sessions s ON s.id = ars.session_id
                WHERE ars.afk_run_id = $1
                ORDER BY ars.started_at NULLS LAST
                """,
                afk_run_id,
            )

    detail = RunDetail(
        run=RunSummary(
            afk_run_id=run_row["afk_run_id"],
            provider=run_row["provider"],
            status=run_row["status"],
            title=run_row["title"],
            started_at=run_row["started_at"],
            finished_at=run_row["finished_at"],
            outcome_status=run_row["outcome_status"],
            first_seen_at=run_row["first_seen_at"],
            last_seen_at=run_row["last_seen_at"],
        ),
        outcome=(
            EngineeringOutcome.model_validate(run_row["outcome"])
            if run_row["outcome"] is not None
            else None
        ),
    )

    grouped: dict[str, list[EntityLink]] = {}
    for row in entity_rows:
        link = _entity_link(row)
        field = _ENTITY_TYPE_FIELDS.get(row["entity_type"])
        if field is None:
            continue
        grouped.setdefault(field, []).append(link)
    detail.issues = grouped.get("issues", [])
    detail.change_requests = grouped.get("change_requests", [])
    detail.reviews = grouped.get("reviews", [])
    detail.commits = grouped.get("commits", [])
    detail.merge_events = grouped.get("merge_events", [])

    sessions: list[SessionLink] = []
    agents: set[str] = set()
    totals = {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
        "message_count": 0,
        "session_count": 0,
    }
    cost: Decimal | None = None
    for row in session_rows:
        sessions.append(
            SessionLink(
                session_id=str(row["session_id"]) if row["session_id"] else None,
                external_session_id=row["external_session_id"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                inferred=True,
                agent=row["agent"],
                message_count=row["message_count"] or 0,
                total_input_tokens=row["total_input_tokens"] or 0,
                total_output_tokens=row["total_output_tokens"] or 0,
                total_cache_read_tokens=row["total_cache_read_tokens"] or 0,
                total_cache_write_tokens=row["total_cache_write_tokens"] or 0,
                total_estimated_cost_usd=row["total_estimated_cost_usd"],
                parent_session_id=row.get("parent_session_id"),
            )
        )
        if row["agent"]:
            agents.add(row["agent"])
        totals["input"] += row["total_input_tokens"] or 0
        totals["output"] += row["total_output_tokens"] or 0
        totals["cache_read"] += row["total_cache_read_tokens"] or 0
        totals["cache_write"] += row["total_cache_write_tokens"] or 0
        totals["message_count"] += row["message_count"] or 0
        totals["session_count"] += 1
        if row["total_estimated_cost_usd"] is not None:
            cost = (cost or Decimal("0")) + row["total_estimated_cost_usd"]

    detail.sessions = sessions
    detail.agents = sorted(agents)
    detail.usage = UsageAggregate(
        active_tokens=totals["input"] + totals["output"],
        input_tokens=totals["input"],
        output_tokens=totals["output"],
        cache_read_tokens=totals["cache_read"],
        cache_write_tokens=totals["cache_write"],
        estimated_cost_usd=cost,
        message_count=totals["message_count"],
        session_count=totals["session_count"],
    )
    return detail


async def _fetch_entities(
    conn: asyncpg.Connection,
    limit: int,
    offset: int,
    *,
    db_timeout_seconds: int,
) -> PaginatedResponse[EntityRow]:
    """Execute count + data queries for the list-entities endpoint."""
    async with timed_operation("db.query.afk.entities.count", "db"):
        async with _db_timeout("db.query.afk.entities.count", db_timeout_seconds):
            total = await conn.fetchval("SELECT COUNT(*) FROM afk_run_entities")

    data_sql = """
        SELECT afk_run_id, provider, repository, entity_type, external_id, role,
               correlation_method, correlation_confidence, evidence,
               resolver_version, owning_change_request_id, correlation_source,
               superseded_at
        FROM afk_run_entities
        ORDER BY entity_type, external_id, afk_run_id
        LIMIT $1 OFFSET $2
    """
    async with timed_operation("db.query.afk.entities.data", "db"):
        async with _db_timeout("db.query.afk.entities.data", db_timeout_seconds):
            rows = await conn.fetch(data_sql, limit, offset)

    items = [
        EntityRow(
            entity_id=f"{r['entity_type']}:{r['external_id']}",
            entity_type=r["entity_type"],
            external_id=r["external_id"],
            provider=r["provider"],
            repository=r["repository"],
            afk_run_id=r["afk_run_id"],
            role=r["role"],
            correlation_method=r["correlation_method"],
            correlation_confidence=r["correlation_confidence"],
            evidence=_parse_evidence(r["evidence"]),
            resolver_version=r["resolver_version"],
            owning_change_request_id=r["owning_change_request_id"],
            correlation_source=r["correlation_source"],
            superseded_at=r["superseded_at"],
            provisional=r["role"] != _RESOLVED_ROLE,
        )
        for r in rows
    ]
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


async def _fetch_correlations(
    conn: asyncpg.Connection,
    limit: int,
    offset: int,
    *,
    reason: str | None,
    db_timeout_seconds: int,
) -> PaginatedResponse[UnresolvedCorrelationRow]:
    """Execute count + data queries for the list-correlations endpoint."""
    params: list[object] = []
    where = "TRUE"
    if reason is not None:
        where = f"reason = ${len(params) + 1}"
        params.append(reason)

    async with timed_operation("db.query.afk.correlations.count", "db"):
        async with _db_timeout("db.query.afk.correlations.count", db_timeout_seconds):
            total = await conn.fetchval(
                f"SELECT COUNT(*) FROM unresolved_correlations WHERE {where}", *params
            )

    data_sql = f"""
        SELECT provider, repository, entity_type, external_id, afk_run_id, method,
               reason, correlation_confidence, candidates, evidence, resolver_version,
               created_at
        FROM unresolved_correlations
        WHERE {where}
        ORDER BY created_at DESC, entity_type, external_id
        LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
    """
    async with timed_operation("db.query.afk.correlations.data", "db"):
        async with _db_timeout("db.query.afk.correlations.data", db_timeout_seconds):
            rows = await conn.fetch(data_sql, *params, limit, offset)

    items = [
        UnresolvedCorrelationRow(
            entity_id=f"{r['entity_type']}:{r['external_id']}",
            entity_type=r["entity_type"],
            external_id=r["external_id"],
            provider=r["provider"],
            repository=r["repository"],
            afk_run_id=r["afk_run_id"],
            method=r["method"],
            reason=r["reason"],
            correlation_confidence=r["correlation_confidence"],
            candidates=r["candidates"] or [],
            evidence=_parse_evidence(r["evidence"]),
            resolver_version=r["resolver_version"],
            created_at=r["created_at"],
            provisional=True,
        )
        for r in rows
    ]
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


# ── Change-request summary query (issue #610) ───────────────────────────────

# The change-request summary derives one row per flattened stable resource
# identity ``(provider, repository, external_id)`` from three sources:
#
# * ``engineering_events`` — ``change_request`` facts (provider-state source);
# * ``afk_runs`` / ``afk_run_entities`` — the owning lifecycle (automation
#   state and, through ``afk_run_sessions`` → ``sessions``, cost);
# * ``execution_bindings`` — AWX execution counts, only for bindings with a
#   durable change-request identity (every resource-identity column present).
#
# Aggregation rules are deterministic and documented on the SQL below:
# provider state prefers ``merged`` over ``closed`` over ``open`` (observed
# facts only — never a provider API claim); automation state mirrors
# ``resolve_afk_run_status``'s success-aware precedence; cost is a plain SUM
# that stays NULL when no linked session carries cost telemetry (unavailable,
# never zero).

#: Provider lifecycle fact types that denote an open change request.
_OPEN_EVENT_TYPES = (
    "'change_request.opened', 'change_request.updated', 'change_request.reopened'"
)

#: The grouped aggregation body shared by the count and data queries.  Built
#: with the identity filters (``{inner_where}``) applied pre-aggregation;
#: state/activity filters are applied on the outer ``summary`` wrapper.
_CHANGE_REQUEST_GROUPED_SQL = """
        SELECT
            i.provider AS provider,
            i.repository AS repository,
            i.external_id AS external_id,
            CASE
                WHEN BOOL_OR(es.merged) THEN 'merged'
                WHEN BOOL_OR(es.closed) THEN 'closed'
                WHEN BOOL_OR(es.opened) THEN 'open'
                ELSE NULL
            END AS provider_state,
            CASE
                WHEN BOOL_OR(r.status = 'running') THEN 'running'
                WHEN BOOL_OR(r.status = 'completed') THEN 'completed'
                WHEN BOOL_OR(r.status = 'failed') THEN 'failed'
                WHEN BOOL_OR(r.status = 'cancelled') THEN 'cancelled'
                WHEN BOOL_OR(r.status = 'pending') THEN 'pending'
                ELSE NULL
            END AS automation_state,
            COALESCE(
                MAX(r.last_seen_at),
                MAX(es.latest_event_at),
                MAX(ec.latest_exec_at)
            ) AS latest_activity_at,
            COALESCE(ec.total, 0) AS execution_total,
            COALESCE(ec.running, 0) AS execution_running,
            COALESCE(ec.completed, 0) AS execution_completed,
            COALESCE(ec.failed, 0) AS execution_failed,
            COALESCE(ec.cancelled, 0) AS execution_cancelled,
            SUM(s.total_estimated_cost_usd) AS total_estimated_cost_usd
        FROM identities i
        LEFT JOIN event_state es
            ON es.provider = i.provider
           AND es.repository = i.repository
           AND es.external_id = i.external_id
        LEFT JOIN run_refs rr
            ON rr.provider = i.provider
           AND rr.repository = i.repository
           AND rr.external_id = i.external_id
        LEFT JOIN afk_runs r ON r.afk_run_id = rr.afk_run_id
        LEFT JOIN afk_run_sessions ars ON ars.afk_run_id = r.afk_run_id
        LEFT JOIN sessions s ON s.id = ars.session_id
        LEFT JOIN exec_counts ec
            ON ec.provider = i.provider
           AND ec.repository = i.repository
           AND ec.external_id = i.external_id
        {inner_where}
        GROUP BY i.provider, i.repository, i.external_id,
                 es.merged, es.closed, es.opened, es.latest_event_at,
                 ec.total, ec.running, ec.completed, ec.failed, ec.cancelled,
                 ec.latest_exec_at
"""

_CHANGE_REQUEST_CTES = """
    WITH identities AS (
        SELECT provider, repository, external_id
        FROM engineering_events
        WHERE entity_type = 'change_request'
        UNION
        SELECT change_request_provider, change_request_repository,
               change_request_external_id
        FROM afk_runs
        WHERE change_request_provider IS NOT NULL
          AND change_request_repository IS NOT NULL
          AND change_request_external_id IS NOT NULL
        UNION
        SELECT provider, repository_url, entity_number
        FROM execution_bindings
        WHERE entity_type = 'change_request'
          AND provider IS NOT NULL
          AND repository_url IS NOT NULL
          AND entity_number IS NOT NULL
    ),
    run_refs AS (
        SELECT change_request_provider AS provider,
               change_request_repository AS repository,
               change_request_external_id AS external_id,
               afk_run_id
        FROM afk_runs
        WHERE change_request_provider IS NOT NULL
          AND change_request_repository IS NOT NULL
          AND change_request_external_id IS NOT NULL
        UNION
        SELECT provider, repository, external_id, afk_run_id
        FROM afk_run_entities
        WHERE entity_type = 'change_request'
    ),
    event_state AS (
        SELECT provider, repository, external_id,
               BOOL_OR(event_type = 'change_request.merged') AS merged,
               BOOL_OR(event_type = 'change_request.closed') AS closed,
               BOOL_OR(event_type IN ({open_types})) AS opened,
               MAX(occurred_at) AS latest_event_at
        FROM engineering_events
        WHERE entity_type = 'change_request'
        GROUP BY provider, repository, external_id
    ),
    exec_counts AS (
        SELECT provider, repository_url AS repository, entity_number AS external_id,
               COUNT(*) AS total,
               COUNT(*) FILTER (WHERE outcome = 'running') AS running,
               COUNT(*) FILTER (WHERE outcome = 'completed') AS completed,
               COUNT(*) FILTER (WHERE outcome = 'failed') AS failed,
               COUNT(*) FILTER (WHERE outcome = 'cancelled') AS cancelled,
               MAX(COALESCE(finished_at, started_at, created_at)) AS latest_exec_at
        FROM execution_bindings
        WHERE entity_type = 'change_request'
          AND provider IS NOT NULL
          AND repository_url IS NOT NULL
          AND entity_number IS NOT NULL
        GROUP BY provider, repository_url, entity_number
    )
"""


def _build_change_request_queries(
    provider: str | None,
    repository: str | None,
    provider_state: str | None,
    automation_state: str | None,
    activity_from: datetime | None,
    activity_to: datetime | None,
) -> tuple[str, str, list[object]]:
    """Build the count and data SQL (plus params) for the change-request summary.

    Identity filters (provider, repository) apply pre-aggregation on the
    ``identities`` CTE; derived-state filters (provider state, automation
    state) and the activity window apply post-aggregation on the ``summary``
    wrapper, so the paginated ``total`` reflects the full filter set.
    """
    params: list[object] = []
    inner_filters: list[str] = []
    if provider is not None:
        inner_filters.append(f"i.provider = ${len(params) + 1}")
        params.append(provider)
    if repository is not None:
        inner_filters.append(f"i.repository = ${len(params) + 1}")
        params.append(repository)
    inner_where = f"WHERE {' AND '.join(inner_filters)}" if inner_filters else ""

    post_filters: list[str] = []
    if provider_state is not None:
        post_filters.append(f"summary.provider_state = ${len(params) + 1}")
        params.append(provider_state)
    if automation_state is not None:
        post_filters.append(f"summary.automation_state = ${len(params) + 1}")
        params.append(automation_state)
    if activity_from is not None:
        post_filters.append(f"summary.latest_activity_at >= ${len(params) + 1}")
        params.append(activity_from)
    if activity_to is not None:
        post_filters.append(f"summary.latest_activity_at <= ${len(params) + 1}")
        params.append(activity_to)
    post_where = f"WHERE {' AND '.join(post_filters)}" if post_filters else ""

    ctes = _CHANGE_REQUEST_CTES.format(open_types=_OPEN_EVENT_TYPES)
    grouped = _CHANGE_REQUEST_GROUPED_SQL.format(inner_where=inner_where)

    count_sql = f"SELECT COUNT(*) FROM (\n{ctes}{grouped}\n) AS summary {post_where}"
    data_sql = f"""
{ctes}SELECT * FROM (
{grouped}
) AS summary
{post_where}
ORDER BY summary.latest_activity_at DESC NULLS LAST,
         summary.provider ASC, summary.repository ASC, summary.external_id ASC
LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
"""
    return count_sql, data_sql, params


def _change_request_summary_row(row: asyncpg.Record) -> ChangeRequestSummaryRow:
    """Build a :class:`ChangeRequestSummaryRow` from an aggregated query row."""
    return ChangeRequestSummaryRow(
        provider=row["provider"],
        repository=row["repository"],
        external_id=row["external_id"],
        provider_state=row["provider_state"],
        automation_state=row["automation_state"],
        total_estimated_cost_usd=row["total_estimated_cost_usd"],
        latest_linked_activity=row["latest_activity_at"],
        executions=ChangeRequestExecutionCounts(
            total=row["execution_total"] or 0,
            running=row["execution_running"] or 0,
            completed=row["execution_completed"] or 0,
            failed=row["execution_failed"] or 0,
            cancelled=row["execution_cancelled"] or 0,
        ),
    )


async def _fetch_change_request_summaries(
    conn: asyncpg.Connection,
    provider: str | None,
    repository: str | None,
    provider_state: str | None,
    automation_state: str | None,
    activity_from: datetime | None,
    activity_to: datetime | None,
    limit: int,
    offset: int,
    *,
    db_timeout_seconds: int,
) -> PaginatedResponse[ChangeRequestSummaryRow]:
    """Execute count + data queries for the change-request summary endpoint."""
    count_sql, data_sql, params = _build_change_request_queries(
        provider,
        repository,
        provider_state,
        automation_state,
        activity_from,
        activity_to,
    )

    async with timed_operation("db.query.afk.change_requests.count", "db"):
        async with _db_timeout("db.query.afk.change_requests.count", db_timeout_seconds):
            total = await conn.fetchval(count_sql, *params)

    async with timed_operation("db.query.afk.change_requests.data", "db"):
        async with _db_timeout("db.query.afk.change_requests.data", db_timeout_seconds):
            rows = await conn.fetch(data_sql, *params, limit, offset)

    items = [_change_request_summary_row(r) for r in rows]
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


# ═══════════════════════════════════════════════════════════════════════════
#  Endpoints
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/runs")
async def list_runs(
    request: Request,
    repository: str | None = Query(default=None),
    started_from: str | None = Query(default=None),
    started_to: str | None = Query(default=None),
    finished_from: str | None = Query(default=None),
    finished_to: str | None = Query(default=None),
    seen_from: str | None = Query(default=None),
    seen_to: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    outcome: str | None = Query(default=None),
    origin: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    conn: asyncpg.Connection = Depends(get_session),
) -> PaginatedResponse[RunSummary]:
    """List AFK runs, filterable by repository, window, status, outcome, origin."""
    _require_enum_value(status_filter, _VALID_STATUS, "status")
    _require_enum_value(outcome, _VALID_OUTCOME, "outcome")
    _require_enum_value(origin, _VALID_ORIGIN, "origin")

    started_from_dt = _parse_datetime(started_from, "started_from")
    started_to_dt = _parse_datetime(started_to, "started_to")
    finished_from_dt = _parse_datetime(finished_from, "finished_from")
    finished_to_dt = _parse_datetime(finished_to, "finished_to")
    seen_from_dt = _parse_datetime(seen_from, "seen_from")
    seen_to_dt = _parse_datetime(seen_to, "seen_to")

    _validate_window(started_from_dt, started_to_dt, "started_from")
    _validate_window(finished_from_dt, finished_to_dt, "finished_from")
    _validate_window(seen_from_dt, seen_to_dt, "seen_from")

    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        return await _fetch_runs(
            conn,
            repository,
            started_from_dt,
            started_to_dt,
            finished_from_dt,
            finished_to_dt,
            seen_from_dt,
            seen_to_dt,
            status_filter,
            outcome,
            origin,
            limit,
            offset,
            db_timeout_seconds=settings.database_timeout_seconds,
        )


@router.get("/runs/{afk_run_id}")
async def get_run_detail(
    request: Request,
    afk_run_id: str,
    conn: asyncpg.Connection = Depends(get_session),
) -> RunDetail:
    """Return the full chain for one AFK run, with per-link provenance."""
    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        detail = await _fetch_run_detail(
            conn,
            afk_run_id,
            db_timeout_seconds=settings.database_timeout_seconds,
        )
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"AFK run not found: {afk_run_id}",
        )
    return detail


@router.get("/entities")
async def list_entities(
    request: Request,
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    conn: asyncpg.Connection = Depends(get_session),
) -> PaginatedResponse[EntityRow]:
    """List engineering entities with their run links, provenance, superseded state."""
    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        return await _fetch_entities(
            conn,
            limit,
            offset,
            db_timeout_seconds=settings.database_timeout_seconds,
        )


@router.get("/correlations")
async def list_correlations(
    request: Request,
    reason: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    conn: asyncpg.Connection = Depends(get_session),
) -> PaginatedResponse[UnresolvedCorrelationRow]:
    """List unresolved correlations (low-confidence links + ambiguous/unmatched)."""
    _require_enum_value(reason, _VALID_REASON, "reason")
    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        return await _fetch_correlations(
            conn,
            limit,
            offset,
            reason=reason,
            db_timeout_seconds=settings.database_timeout_seconds,
        )


@router.get("/change-requests")
async def list_change_requests(
    request: Request,
    provider: str | None = Query(default=None),
    repository: str | None = Query(default=None),
    provider_state: str | None = Query(default=None),
    automation_state: str | None = Query(default=None),
    activity_from: str | None = Query(default=None),
    activity_to: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    conn: asyncpg.Connection = Depends(get_session),
) -> PaginatedResponse[ChangeRequestSummaryRow]:
    """List change-request summaries — one row per provider/repository/identity.

    Each row aggregates provider state (derived from observed facts),
    AFK automation state, total estimated USD cost (``null`` when no cost
    telemetry is available — never zero), latest linked activity, and AWX
    execution counts.  Executions without a durable change-request identity
    are excluded from the row universe and never contribute counts.
    """
    _require_enum_value(provider, _VALID_ORIGIN, "provider")
    _require_enum_value(provider_state, _VALID_PROVIDER_STATE, "provider_state")
    _require_enum_value(automation_state, _VALID_AUTOMATION_STATE, "automation_state")

    activity_from_dt = _parse_datetime(activity_from, "activity_from")
    activity_to_dt = _parse_datetime(activity_to, "activity_to")
    _validate_window(activity_from_dt, activity_to_dt, "activity_from")

    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        return await _fetch_change_request_summaries(
            conn,
            provider,
            repository,
            provider_state,
            automation_state,
            activity_from_dt,
            activity_to_dt,
            limit,
            offset,
            db_timeout_seconds=settings.database_timeout_seconds,
        )
