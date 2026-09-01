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
- ``GET /change-requests/{provider}/{repository}/{external_number}`` — the
  provider-scoped change-request detail (issue #611): the summary block plus
  linked AFK runs (with link provenance), ordered AWX execution bindings
  (purpose, per-execution session telemetry, cost, duration), deduplicated
  linked sessions, aggregate usage/cost, provider merge state, and the
  optional provenance timeline.

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
    AWXJobIdentity,
    CorrelationEvidence,
    EngineeringOutcome,
    EngineeringOutcomeStatus,
    Provider,
    RunStatus,
    UnresolvedReason,
)
from app.core.config import get_settings
from app.core.schemas.afk import (
    ChangeRequestDetail,
    ChangeRequestDetailSummary,
    ChangeRequestExecutionCounts,
    ChangeRequestExecutionItem,
    ChangeRequestLinkedRun,
    ChangeRequestMergeState,
    ChangeRequestSummaryRow,
    ChangeRequestTimeline,
    ChangeRequestTimelineEvent,
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


def _parse_job_template_ids(raw: str) -> list[int]:
    """Parse a comma-separated AWX job-template-id list from settings.

    Empty/whitespace-only input yields ``[]`` (purpose unavailable).  Non-
    integer tokens are skipped rather than raising — a single misconfigured
    ID must not take down the read path; the remaining IDs still classify.
    """
    if not raw or not raw.strip():
        return []
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return ids


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
# provider state is derived from the chronologically latest observed
# ``change_request`` lifecycle fact (``merged`` / ``closed`` / ``open`` —
# observed facts only, never a provider API claim), with the historical
# ``merged > closed > open`` precedence retained only as the deterministic
# tie-breaker for equal ``occurred_at`` timestamps; automation state mirrors
# ``resolve_afk_run_status``'s success-aware precedence; cost is summed
# over **deduplicated** linked sessions (one internal session UUID per
# change-request identity — a retry that reuses the same session across
# runs must never double-count) and stays NULL when no linked session
# carries cost telemetry (unavailable, never zero).

#: Provider lifecycle fact types that denote an open change request.
_OPEN_EVENT_TYPES = (
    "'change_request.opened', 'change_request.updated', 'change_request.reopened'"
)

#: Event type → provider state for the chronologically latest lifecycle fact.
#: The deterministic ``MAX(...) >= ...`` tie-break (equal ``occurred_at``)
#: prefers ``merged`` over ``closed`` over ``open`` (issue #613).
_PROVIDER_STATE_SQL = """
            CASE
                WHEN es.latest_merged_at IS NOT NULL
                 AND es.latest_merged_at >= GREATEST(
                     COALESCE(es.latest_closed_at, es.latest_merged_at),
                     COALESCE(es.latest_opened_at, es.latest_merged_at))
                    THEN 'merged'
                WHEN es.latest_closed_at IS NOT NULL
                 AND es.latest_closed_at >= COALESCE(es.latest_opened_at, es.latest_closed_at)
                    THEN 'closed'
                WHEN es.latest_opened_at IS NOT NULL THEN 'open'
                ELSE NULL
            END
"""

#: The grouped aggregation body shared by the count and data queries.  Built
#: with the identity filters (``{inner_where}``) applied pre-aggregation;
#: state/activity filters are applied on the outer ``summary`` wrapper.
_CHANGE_REQUEST_GROUPED_SQL = """
        SELECT
            i.provider AS provider,
            i.repository AS repository,
            i.external_id AS external_id,
{provider_state_sql}            AS provider_state,
            CASE
                WHEN BOOL_OR(r.status = 'running') THEN 'running'
                WHEN BOOL_OR(r.status = 'completed') THEN 'completed'
                WHEN BOOL_OR(r.status = 'failed') THEN 'failed'
                WHEN BOOL_OR(r.status = 'cancelled') THEN 'cancelled'
                WHEN BOOL_OR(r.status = 'pending') THEN 'pending'
                ELSE NULL
            END AS automation_state,
            GREATEST(
                MAX(r.last_seen_at),
                MAX(es.latest_event_at),
                MAX(ec.latest_exec_at)
            ) AS latest_activity_at,
            MAX(es.latest_event_at) AS provider_state_observed_at,
            MAX(ec.latest_exec_at) AS latest_execution_activity,
            COALESCE(ec.total, 0) AS execution_total,
            COALESCE(ec.running, 0) AS execution_running,
            COALESCE(ec.completed, 0) AS execution_completed,
            COALESCE(ec.failed, 0) AS execution_failed,
            COALESCE(ec.cancelled, 0) AS execution_cancelled,
            (
                SELECT ts.total_estimated_cost_usd
                FROM session_cost ts
                WHERE ts.provider = i.provider
                  AND ts.repository = i.repository
                  AND ts.external_id = i.external_id
            ) AS total_estimated_cost_usd
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
        LEFT JOIN exec_counts ec
            ON ec.provider = i.provider
           AND ec.repository = i.repository
           AND ec.external_id = i.external_id
        {inner_where}
        GROUP BY i.provider, i.repository, i.external_id,
                 es.latest_merged_at, es.latest_closed_at, es.latest_opened_at,
                 es.latest_event_at,
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
    session_cost AS (
        SELECT sc.provider, sc.repository, sc.external_id,
               SUM(sess.total_estimated_cost_usd) AS total_estimated_cost_usd
        FROM (
            SELECT DISTINCT rr.provider, rr.repository, rr.external_id,
                            ars.session_id AS session_id
            FROM run_refs rr
            JOIN afk_runs r ON r.afk_run_id = rr.afk_run_id
            JOIN afk_run_sessions ars ON ars.afk_run_id = r.afk_run_id
            WHERE ars.session_id IS NOT NULL
        ) sc
        LEFT JOIN sessions sess ON sess.id = sc.session_id
        GROUP BY sc.provider, sc.repository, sc.external_id
    ),
    event_state AS (
        SELECT provider, repository, external_id,
               MAX(occurred_at) FILTER (WHERE event_type = 'change_request.merged')
                   AS latest_merged_at,
               MAX(occurred_at) FILTER (WHERE event_type = 'change_request.closed')
                   AS latest_closed_at,
               MAX(occurred_at) FILTER (WHERE event_type IN ({open_types}))
                   AS latest_opened_at,
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
    grouped = _CHANGE_REQUEST_GROUPED_SQL.format(
        inner_where=inner_where, provider_state_sql=_PROVIDER_STATE_SQL
    )

    count_sql = f"SELECT COUNT(*) FROM (\n{ctes}{grouped}\n) AS summary {post_where}"
    data_sql = f"""
{ctes}SELECT * FROM (
{grouped}
) AS summary
{post_where}
ORDER BY summary.latest_execution_activity DESC NULLS LAST,
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
        provider_state_observed_at=row["provider_state_observed_at"],
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


# ── Change-request detail query (issue #611) ────────────────────────────────

# The provider-scoped detail resolves one flattened stable resource identity
# ``(provider, repository, external_id)`` across the same three durable
# identity sources as the summary (observed facts, explicit change-request
# bindings on ``afk_runs``, and execution bindings) and composes the full
# change-request lifecycle: the summary block (with merge/freshness
# enrichment), every linked AFK run (with its link provenance), the ordered
# AWX execution bindings (with per-execution session telemetry), the
# deduplicated linked sessions and usage/cost aggregate, and the optional
# provenance timeline.  An identity known to none of the three sources is a
# not-found.
#
# Aggregation rules mirror the summary query: provider state prefers
# ``merged`` over ``closed`` over ``open`` (observed facts only — never a
# provider API claim); automation state mirrors ``resolve_afk_run_status``'s
# success-aware precedence; cost is summed over deduplicated linked sessions
# (one internal session UUID — a retry that reuses the same session across
# runs must never double-count) and stays NULL when no linked session
# carries cost telemetry (unavailable, never zero).
#
# Execution purpose is surfaced only when an explicit stored signal carries
# it, with a fixed precedence:
#
# 1. ``retry`` — a binding whose execution (or owning run) is an explicitly
#    recorded re-attempt (``trigger_type = 'recovery'`` or a run with a
#    ``recovered_from_afk_run_id``);
# 2. ``implementation`` — the binding's AWX ``job_template_id`` is in the
#    operator-configured implementation template set
#    (``GATEWAY_AFK_IMPLEMENTATION_JOB_TEMPLATE_IDS`` — the develop-loop
#    runner);
# 3. ``review`` — the binding's ``job_template_id`` is in the configured
#    review template set (``GATEWAY_AFK_REVIEW_JOB_TEMPLATE_IDS`` — the
#    review runner);
# 4. otherwise unavailable (NULL) — the Gateway never invents a purpose
#    from data that does not carry it.

#: The three durable run-linkage paths for one change-request identity.
#: Embedded as the ``run_sources`` CTE body in the queries below.
_CHANGE_REQUEST_DETAIL_RUN_SOURCES_BODY = """
            SELECT afk_run_id, 'change_request_binding' AS link_source
            FROM afk_runs
            WHERE change_request_provider = $1
              AND change_request_repository = $2
              AND change_request_external_id = $3
            UNION ALL
            SELECT afk_run_id, 'entity_link'
            FROM afk_run_entities
            WHERE entity_type = 'change_request'
              AND provider = $1
              AND repository = $2
              AND external_id = $3
            UNION ALL
            SELECT afk_run_id, 'execution'
            FROM execution_bindings
            WHERE entity_type = 'change_request'
              AND provider = $1
              AND repository_url = $2
              AND entity_number = $3
              AND afk_run_id IS NOT NULL
"""

#: Identity existence across the three durable identity sources.
_CHANGE_REQUEST_DETAIL_EXISTS_SQL = """
    SELECT EXISTS (
        SELECT 1 FROM engineering_events
        WHERE entity_type = 'change_request'
          AND provider = $1 AND repository = $2 AND external_id = $3
        UNION ALL
        SELECT 1 FROM afk_runs
        WHERE change_request_provider = $1
          AND change_request_repository = $2
          AND change_request_external_id = $3
        UNION ALL
        SELECT 1 FROM execution_bindings
        WHERE entity_type = 'change_request'
          AND provider = $1 AND repository_url = $2 AND entity_number = $3
    )
"""

_MATCHED_JOBS_BODY = """\
        SELECT eb.awx_job_id
        FROM run_ids ri
        JOIN execution_bindings eb ON eb.afk_run_id = ri.afk_run_id
        UNION
        SELECT eb.awx_job_id
        FROM execution_bindings eb
        WHERE eb.entity_type = 'change_request'
          AND eb.provider = $1
          AND eb.repository_url = $2
          AND eb.entity_number = $3\
"""

_CHANGE_REQUEST_DETAIL_SUMMARY_SQL = f"""
    WITH run_sources AS (
{_CHANGE_REQUEST_DETAIL_RUN_SOURCES_BODY}
    ),
    run_ids AS (
        SELECT afk_run_id FROM run_sources GROUP BY afk_run_id
    ),
    event_state AS (
        SELECT
            MAX(occurred_at) FILTER (WHERE event_type = 'change_request.merged')
                AS latest_merged_at,
            MAX(occurred_at) FILTER (WHERE event_type = 'change_request.closed')
                AS latest_closed_at,
            MAX(occurred_at) FILTER (WHERE event_type IN ({_OPEN_EVENT_TYPES}))
                AS latest_opened_at,
            MAX(occurred_at) FILTER (WHERE event_type = 'change_request.merged')
                AS merged_at,
            MAX(occurred_at) AS latest_event_at
        FROM engineering_events
        WHERE entity_type = 'change_request'
          AND provider = $1 AND repository = $2 AND external_id = $3
    ),
    matched_jobs AS (
{_MATCHED_JOBS_BODY}
    ),
    exec_counts AS (
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE eb.outcome = 'running') AS running,
            COUNT(*) FILTER (WHERE eb.outcome = 'completed') AS completed,
            COUNT(*) FILTER (WHERE eb.outcome = 'failed') AS failed,
            COUNT(*) FILTER (WHERE eb.outcome = 'cancelled') AS cancelled,
            MAX(COALESCE(eb.finished_at, eb.started_at, eb.created_at)) AS latest_exec_at
        FROM matched_jobs m
        JOIN execution_bindings eb ON eb.awx_job_id = m.awx_job_id
    ),
    latest_binding_titles AS (
        SELECT title
        FROM execution_bindings
        WHERE entity_type = 'change_request'
          AND provider = $1 AND repository_url = $2 AND entity_number = $3
          AND title IS NOT NULL
        ORDER BY COALESCE(started_at, created_at) DESC, awx_job_id DESC
        LIMIT 1
    ),
    latest_run_titles AS (
        SELECT r.title
        FROM run_ids ri
        JOIN afk_runs r ON r.afk_run_id = ri.afk_run_id
        WHERE r.title IS NOT NULL
        ORDER BY r.last_seen_at DESC, r.afk_run_id DESC
        LIMIT 1
    ),
    session_cost AS (
        SELECT SUM(sess.total_estimated_cost_usd) AS total_estimated_cost_usd
        FROM (
            SELECT DISTINCT ars.session_id AS session_id
            FROM run_ids ri
            JOIN afk_runs r ON r.afk_run_id = ri.afk_run_id
            JOIN afk_run_sessions ars ON ars.afk_run_id = r.afk_run_id
            WHERE ars.session_id IS NOT NULL
        ) sc
        LEFT JOIN sessions sess ON sess.id = sc.session_id
    )
    SELECT
        $1::text AS provider,
        $2::text AS repository,
        $3::text AS external_id,
{_PROVIDER_STATE_SQL}        AS provider_state,
        CASE
            WHEN BOOL_OR(r.status = 'running') THEN 'running'
            WHEN BOOL_OR(r.status = 'completed') THEN 'completed'
            WHEN BOOL_OR(r.status = 'failed') THEN 'failed'
            WHEN BOOL_OR(r.status = 'cancelled') THEN 'cancelled'
            WHEN BOOL_OR(r.status = 'pending') THEN 'pending'
            ELSE NULL
        END AS automation_state,
        GREATEST(
            MAX(r.last_seen_at),
            MAX(es.latest_event_at),
            MAX(ec.latest_exec_at)
        ) AS latest_activity_at,
        COALESCE(MAX(ec.total), 0) AS execution_total,
        COALESCE(MAX(ec.running), 0) AS execution_running,
        COALESCE(MAX(ec.completed), 0) AS execution_completed,
        COALESCE(MAX(ec.failed), 0) AS execution_failed,
        COALESCE(MAX(ec.cancelled), 0) AS execution_cancelled,
        (SELECT total_estimated_cost_usd FROM session_cost)
            AS total_estimated_cost_usd,
        MAX(es.merged_at) AS merged_at,
        MAX(es.latest_event_at) AS provider_state_observed_at,
        COALESCE(
            (SELECT title FROM latest_binding_titles),
            (SELECT title FROM latest_run_titles)
        ) AS title
    FROM (VALUES (1)) AS seed(n)
    LEFT JOIN run_ids ri ON TRUE
    LEFT JOIN afk_runs r ON r.afk_run_id = ri.afk_run_id
    CROSS JOIN event_state es
    CROSS JOIN exec_counts ec
    GROUP BY es.latest_merged_at, es.latest_closed_at, es.latest_opened_at,
             es.latest_event_at, es.merged_at,
             ec.total, ec.running, ec.completed, ec.failed, ec.cancelled,
             ec.latest_exec_at
"""

_CHANGE_REQUEST_DETAIL_RUNS_SQL = f"""
    WITH run_sources AS (
{_CHANGE_REQUEST_DETAIL_RUN_SOURCES_BODY}
    )
    SELECT r.afk_run_id, r.provider, r.status, r.title, r.started_at,
           r.finished_at, r.outcome_status, r.first_seen_at, r.last_seen_at,
           ARRAY_AGG(DISTINCT rs.link_source ORDER BY rs.link_source)
               AS link_sources
    FROM run_sources rs
    JOIN afk_runs r ON r.afk_run_id = rs.afk_run_id
    GROUP BY r.afk_run_id, r.provider, r.status, r.title, r.started_at,
             r.finished_at, r.outcome_status, r.first_seen_at, r.last_seen_at
    ORDER BY r.last_seen_at DESC NULLS LAST, r.afk_run_id ASC
"""

_CHANGE_REQUEST_DETAIL_EXECUTIONS_SQL = f"""
    WITH run_sources AS (
{_CHANGE_REQUEST_DETAIL_RUN_SOURCES_BODY}
    ),
    run_ids AS (
        SELECT afk_run_id FROM run_sources GROUP BY afk_run_id
    ),
    matched_jobs AS (
{_MATCHED_JOBS_BODY}
    )
    SELECT eb.awx_job_id, eb.job_template_id, eb.external_session_id,
           eb.afk_run_id, eb.outcome, eb.trigger_type, eb.source_event_id,
           eb.branch, eb.title, eb.started_at, eb.finished_at,
           eb.failure_reason, eb.failure_summary,
           CASE
               WHEN eb.trigger_type = 'recovery'
                 OR r.trigger_type = 'recovery'
                 OR r.recovered_from_afk_run_id IS NOT NULL
               THEN 'retry'
               WHEN eb.job_template_id = ANY($4::bigint[])
               THEN 'implementation'
               WHEN eb.job_template_id = ANY($5::bigint[])
               THEN 'review'
               ELSE NULL
           END AS purpose,
           s.id AS session_id,
           s.total_input_tokens,
           s.total_output_tokens,
           s.total_cache_read_tokens,
           s.total_cache_write_tokens,
           s.total_estimated_cost_usd AS estimated_cost_usd
    FROM matched_jobs m
    JOIN execution_bindings eb ON eb.awx_job_id = m.awx_job_id
    LEFT JOIN afk_runs r ON r.afk_run_id = eb.afk_run_id
    LEFT JOIN LATERAL (
        SELECT id, total_input_tokens, total_output_tokens,
               total_cache_read_tokens, total_cache_write_tokens,
               total_estimated_cost_usd
        FROM sessions
        WHERE sessions.external_session_id = eb.external_session_id
          AND eb.external_session_id IS NOT NULL
        ORDER BY sessions.first_message_at DESC
        LIMIT 1
    ) s ON TRUE
    ORDER BY COALESCE(eb.started_at, eb.created_at) ASC, eb.awx_job_id ASC
"""

_CHANGE_REQUEST_DETAIL_SESSIONS_SQL = f"""
    WITH run_sources AS (
{_CHANGE_REQUEST_DETAIL_RUN_SOURCES_BODY}
    )
    SELECT ars.session_id, ars.external_session_id, ars.started_at,
           ars.finished_at, s.agent, s.total_input_tokens,
           s.total_output_tokens, s.total_cache_read_tokens,
           s.total_cache_write_tokens, s.total_estimated_cost_usd,
           s.message_count, s.parent_session_id
    FROM run_sources rs
    JOIN afk_run_sessions ars ON ars.afk_run_id = rs.afk_run_id
    LEFT JOIN sessions s ON s.id = ars.session_id
    ORDER BY ars.started_at NULLS LAST, ars.external_session_id ASC
"""

_CHANGE_REQUEST_DETAIL_TIMELINE_SQL = """
    SELECT event_type, occurred_at, observed_via, snapshot_at, actor
    FROM engineering_events
    WHERE entity_type = 'change_request'
      AND provider = $1
      AND repository = $2
      AND external_id = $3
    ORDER BY occurred_at ASC, observation_key ASC
"""


def _change_request_detail_summary_row(
    row: asyncpg.Record,
) -> ChangeRequestDetailSummary:
    """Build the detail summary block from the aggregated query row."""
    return ChangeRequestDetailSummary(
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
        title=row["title"],
        merged_at=row["merged_at"],
        provider_state_observed_at=row["provider_state_observed_at"],
    )


def _change_request_linked_run_row(row: asyncpg.Record) -> ChangeRequestLinkedRun:
    """Build one linked AFK run row (with link provenance)."""
    return ChangeRequestLinkedRun(
        afk_run_id=row["afk_run_id"],
        provider=row["provider"],
        status=row["status"],
        title=row["title"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        outcome_status=row["outcome_status"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        link_sources=list(row["link_sources"]),
    )


def _change_request_execution_item(
    row: asyncpg.Record,
) -> ChangeRequestExecutionItem:
    """Build one execution item from an ``execution_bindings`` row joined to
    its resolved session (per-execution telemetry)."""
    started_at = row["started_at"]
    finished_at = row["finished_at"]
    duration = None
    if started_at is not None and finished_at is not None:
        duration = (finished_at - started_at).total_seconds()
    return ChangeRequestExecutionItem(
        awx_job=AWXJobIdentity(
            job_id=str(row["awx_job_id"]),
            job_template_id=row["job_template_id"],
        ),
        external_session_id=row["external_session_id"],
        session_id=str(row["session_id"]) if row["session_id"] else None,
        afk_run_id=row["afk_run_id"],
        outcome=row["outcome"],
        purpose=row["purpose"],
        trigger_type=row["trigger_type"],
        source_event_id=row["source_event_id"],
        branch=row["branch"],
        title=row["title"],
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration,
        failure_reason=row["failure_reason"],
        failure_summary=row["failure_summary"],
        total_input_tokens=row["total_input_tokens"],
        total_output_tokens=row["total_output_tokens"],
        total_cache_read_tokens=row["total_cache_read_tokens"],
        total_cache_write_tokens=row["total_cache_write_tokens"],
        estimated_cost_usd=row["estimated_cost_usd"],
    )


def _change_request_timeline_event(
    row: asyncpg.Record,
) -> ChangeRequestTimelineEvent:
    """Build one provenance-timeline event from an ``engineering_events`` fact."""
    return ChangeRequestTimelineEvent(
        event_type=row["event_type"],
        occurred_at=row["occurred_at"],
        observed_via=row["observed_via"],
        snapshot_at=row["snapshot_at"],
        actor=row["actor"],
    )


def _session_link(row: asyncpg.Record) -> SessionLink:
    """Build a :class:`SessionLink` from a joined ``afk_run_sessions`` row."""
    return SessionLink(
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


async def _fetch_change_request_detail(
    conn: asyncpg.Connection,
    provider: str,
    repository: str,
    external_number: str,
    *,
    implementation_template_ids: list[int],
    review_template_ids: list[int],
    db_timeout_seconds: int,
) -> ChangeRequestDetail | None:
    """Compose the provider-scoped change-request detail (issue #611).

    Six bounded queries, no N+1:

    1. identity existence across the three durable identity sources;
    2. the summary block (states, freshness, aggregate cost/counts, title);
    3. linked AFK runs with every durable link source;
    4. ordered AWX execution bindings with per-execution session telemetry;
    5. linked sessions (deduplicated) for the usage/cost aggregate;
    6. the optional provenance-timeline facts.
    """
    async with timed_operation("db.query.afk.change_request.detail.exists", "db"):
        async with _db_timeout(
            "db.query.afk.change_request.detail.exists", db_timeout_seconds
        ):
            exists = await conn.fetchval(
                _CHANGE_REQUEST_DETAIL_EXISTS_SQL,
                provider,
                repository,
                external_number,
            )
    if not exists:
        return None

    async with timed_operation("db.query.afk.change_request.detail.summary", "db"):
        async with _db_timeout(
            "db.query.afk.change_request.detail.summary", db_timeout_seconds
        ):
            summary_row = await conn.fetchrow(
                _CHANGE_REQUEST_DETAIL_SUMMARY_SQL,
                provider,
                repository,
                external_number,
            )

    async with timed_operation("db.query.afk.change_request.detail.runs", "db"):
        async with _db_timeout(
            "db.query.afk.change_request.detail.runs", db_timeout_seconds
        ):
            run_rows = await conn.fetch(
                _CHANGE_REQUEST_DETAIL_RUNS_SQL,
                provider,
                repository,
                external_number,
            )

    async with timed_operation("db.query.afk.change_request.detail.executions", "db"):
        async with _db_timeout(
            "db.query.afk.change_request.detail.executions", db_timeout_seconds
        ):
            execution_rows = await conn.fetch(
                _CHANGE_REQUEST_DETAIL_EXECUTIONS_SQL,
                provider,
                repository,
                external_number,
                implementation_template_ids,
                review_template_ids,
            )

    async with timed_operation("db.query.afk.change_request.detail.sessions", "db"):
        async with _db_timeout(
            "db.query.afk.change_request.detail.sessions", db_timeout_seconds
        ):
            session_rows = await conn.fetch(
                _CHANGE_REQUEST_DETAIL_SESSIONS_SQL,
                provider,
                repository,
                external_number,
            )

    async with timed_operation("db.query.afk.change_request.detail.timeline", "db"):
        async with _db_timeout(
            "db.query.afk.change_request.detail.timeline", db_timeout_seconds
        ):
            timeline_rows = await conn.fetch(
                _CHANGE_REQUEST_DETAIL_TIMELINE_SQL,
                provider,
                repository,
                external_number,
            )

    summary = _change_request_detail_summary_row(summary_row)

    merge_state = None
    if summary.provider_state is not None or summary.merged_at is not None:
        merge_state = ChangeRequestMergeState(
            state="merged" if summary.merged_at is not None else "not_merged",
            merged_at=summary.merged_at,
        )

    sessions: list[SessionLink] = []
    seen_sessions: set[str] = set()
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
        key = (
            str(row["session_id"])
            if row["session_id"] is not None
            else f"external:{row['external_session_id']}"
        )
        if key in seen_sessions:
            continue
        seen_sessions.add(key)
        sessions.append(_session_link(row))
        totals["input"] += row["total_input_tokens"] or 0
        totals["output"] += row["total_output_tokens"] or 0
        totals["cache_read"] += row["total_cache_read_tokens"] or 0
        totals["cache_write"] += row["total_cache_write_tokens"] or 0
        totals["message_count"] += row["message_count"] or 0
        totals["session_count"] += 1
        if row["total_estimated_cost_usd"] is not None:
            cost = (cost or Decimal("0")) + row["total_estimated_cost_usd"]

    return ChangeRequestDetail(
        change_request=summary,
        afk_runs=[_change_request_linked_run_row(r) for r in run_rows],
        executions=[_change_request_execution_item(r) for r in execution_rows],
        sessions=sessions,
        usage=UsageAggregate(
            active_tokens=totals["input"] + totals["output"],
            input_tokens=totals["input"],
            output_tokens=totals["output"],
            cache_read_tokens=totals["cache_read"],
            cache_write_tokens=totals["cache_write"],
            estimated_cost_usd=cost,
            message_count=totals["message_count"],
            session_count=totals["session_count"],
        ),
        total_estimated_cost_usd=summary.total_estimated_cost_usd,
        merge_state=merge_state,
        timeline=(
            ChangeRequestTimeline(
                events=[_change_request_timeline_event(r) for r in timeline_rows]
            )
            if timeline_rows
            else None
        ),
    )


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


@router.get("/change-requests/{provider}/{repository:path}/{external_number}")
async def get_change_request_detail(
    request: Request,
    provider: str,
    repository: str,
    external_number: str,
    conn: asyncpg.Connection = Depends(get_session),
) -> ChangeRequestDetail:
    """Return the provider-scoped change-request detail (issue #611).

    Resolves the change request directly by ``(provider, repository,
    external number)`` — no internal AFK Run ID discovery required — and
    returns one composite read model: the summary block (provider state,
    AFK automation state, merge/freshness enrichment, aggregate cost),
    the linked AFK runs with link provenance, the ordered AWX execution
    bindings (purpose, per-execution session telemetry, cost, duration,
    failure metadata), the deduplicated linked sessions, the aggregate
    usage/cost, and the optional provenance timeline.

    * ``200`` — the detail.
    * ``400`` — invalid provider or an empty repository/external-number
      identity segment.
    * ``404`` — the identity is unknown to every durable source (observed
      facts, explicit change-request bindings, and execution bindings).

    Strictly read-only — reads stored facts/projections only and makes no
    provider API calls (PRD #609).
    """
    _require_enum_value(provider, _VALID_ORIGIN, "provider")
    if not repository or not repository.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Invalid change-request identity: repository must be a "
                "non-empty string"
            ),
        )
    if not external_number or not external_number.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Invalid change-request identity: external number must be a "
                "non-empty string"
            ),
        )

    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        detail = await _fetch_change_request_detail(
            conn,
            provider,
            repository,
            external_number,
            implementation_template_ids=_parse_job_template_ids(
                settings.afk_implementation_job_template_ids
            ),
            review_template_ids=_parse_job_template_ids(
                settings.afk_review_job_template_ids
            ),
            db_timeout_seconds=settings.database_timeout_seconds,
        )
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Change request not found: {provider} {repository} "
                f"#{external_number}"
            ),
        )
    return detail
