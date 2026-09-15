"""Canonical AFK Run REST API (issues #672/#674, contract ``docs/contracts/afk-run-api-v1.md``).

Endpoints under the canonical namespace ``/api/v1/afk/runs``:

- ``GET /runs``        — paginated, filterable list of AFK Runs (§4.1).
- ``GET /runs/{id}``   — canonical detail: the full chain for one run with
  the run block carrying the extended ``AFKRunSummary`` (§4.2).
- ``PATCH /runs/{id}`` — guarded lifecycle update (issue #673): only the
  contract-approved mutable fields (``title``, ``status``) may change,
  terminal runs are frozen (409), ``pending`` runs are patchable
  (provisional), identical values are an idempotent no-op, and the row is
  serialized with ``SELECT ... FOR UPDATE``.
- ``DELETE /runs/{id}`` — orphan-only deletion of one AFK Run (issue #674):
  eligible only with zero execution bindings, no bound change request, and
  zero delivery-log rows; operator-gated.

This is the resource-oriented canonical surface of the AFK Run aggregate.
The write endpoints (PATCH #673, DELETE #674) join on this same router;
creation remains exclusively ``POST /api/v1/afk/executions/runs``.

**Placement (binding contract §1).** These routes deliberately live on a
dedicated router mounted at prefix ``/api/v1/afk`` in
:mod:`app.core.factory` — never on :mod:`app.api.afk_executions` (its
``GET /api/v1/afk/executions/runs/{afk_run_id}`` is the execution-binding
history and must stay unchanged) and never on :mod:`app.api.afk_outcomes`
(read-only by contract).  The read path reuses the outcomes query
composition and schema shapes *additively*: the extended
:class:`~app.core.schemas.afk.AFKRunSummary` / :class:`~app.core.schemas.afk.AFKRunDetail`
exist only in the canonical namespace while ``RunSummary`` / ``RunDetail``
keep their exact current shape on ``/api/v1/afk-outcomes``.

All responses use the shared ``{status, data, error}`` envelope and are
protected by the global :class:`~app.core.auth.ApiKeyMiddleware`.  The read
path follows the ``app/api/afk_outcomes.py`` convention: raw asyncpg via
``Depends(get_session)``, explicit-column SELECTs, parameterised filters
with 400 on invalid enum/date/pagination values, and the
``_db_timeout``/``_request_timeout``/``timed_operation`` helpers.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

import asyncpg
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)

from afk_outcomes.models import (
    CorrelationEvidence,
    EngineeringOutcome,
    EngineeringOutcomeStatus,
    Provider,
    RunStatus,
)
from afk_outcomes.repository import AsyncpgOutcomeRepository
from app.core.auth import require_operator_token
from app.core.config import get_settings
from app.core.schemas.afk import (
    AFKRunChangeRequest,
    AFKRunDetail,
    AFKRunSummary,
    AFKRunUpdateRequest,
    EntityLink,
    SessionLink,
    UsageAggregate,
)
from app.core.schemas.usage import PaginatedResponse
from app.core.telemetry import timed_operation
from app.core.timeouts import db_timeout as _db_timeout
from app.core.timeouts import request_timeout as _request_timeout
from app.api.afk_executions import require_awx_execution_binding_credential
from app.db.session import get_session

router = APIRouter(tags=["afk-runs"])

# ── Valid filter values (locked domain vocabulary) ───────────────────────────

# Contract §3: the status-filter vocabulary covers all EIGHT lifecycle values —
# the seven RunStatus members plus the provisional ``pending`` (which is
# deliberately not a RunStatus member; it enters afk_runs.status only through
# provisioning).
_VALID_STATUS = frozenset(m.value for m in RunStatus) | {"pending"}
_VALID_OUTCOME = frozenset(m.value for m in EngineeringOutcomeStatus)
_VALID_PROVIDER = frozenset(m.value for m in Provider)

# Contract §2 — path-parameter validation: an afk_run_id must match the ULID
# shape (26 Crockford base32 characters; I/L/O/U excluded) before any
# database access — the same deterministic-400 precedent as
# _validate_awx_job_id on the execution-binding paths.
_ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")

# A derived link is provisional (inferred) when its role is not a definitive
# "resolved" — i.e. "referenced" (sub-threshold confidence) or "noise".
_RESOLVED_ROLE = "resolved"

# Entity-type → detail response field name grouping.
_ENTITY_TYPE_FIELDS = {
    "issue": "issues",
    "change_request": "change_requests",
    "review": "reviews",
    "commit": "commits",
    "merge_event": "merge_events",
}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _validate_afk_run_id(afk_run_id: str) -> str:
    """Raise 400 unless *afk_run_id* matches the ULID shape (contract §2)."""
    if not _ULID_RE.fullmatch(afk_run_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid afk_run_id: {afk_run_id!r} is not a valid ULID "
                "(26 Crockford base32 characters)"
            ),
        )
    return afk_run_id


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


def _parse_bool_param(raw: str | None, param_name: str) -> bool | None:
    """Parse a boolean query param, raising 400 on anything but true/false."""
    if raw is None:
        return None
    lowered = raw.strip().lower()
    if lowered in ("true", "1"):
        return True
    if lowered in ("false", "0"):
        return False
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Invalid {param_name}: {raw!r} must be a boolean (true or false)",
    )


def _parse_limit(raw: str | None) -> int:
    """Parse the ``limit`` pagination param (1–1000, default 50) or raise 400."""
    if raw is None:
        return 50
    try:
        value = int(raw)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid limit: {raw!r} must be an integer",
        ) from None
    if not 1 <= value <= 1000:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid limit: {value} must be between 1 and 1000",
        )
    return value


def _parse_offset(raw: str | None) -> int:
    """Parse the ``offset`` pagination param (≥ 0, default 0) or raise 400."""
    if raw is None:
        return 0
    try:
        value = int(raw)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid offset: {raw!r} must be an integer",
        ) from None
    if value < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid offset: {value} must be >= 0",
        )
    return value


def _parse_evidence(raw: object) -> list[CorrelationEvidence]:
    """Parse a JSONB evidence array into :class:`CorrelationEvidence` items."""
    if not raw:
        return []
    if not isinstance(raw, list):
        return []
    return [CorrelationEvidence.model_validate(item) for item in raw]


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


def _afk_run_summary(row: asyncpg.Record) -> AFKRunSummary:
    """Build the canonical :class:`AFKRunSummary` from an ``afk_runs`` row.

    The change-request block is embedded only when the binding columns are
    set (all-set-or-all-None invariant, contract §2) — otherwise ``None``.
    """
    change_request = None
    if row["change_request_provider"] is not None:
        change_request = AFKRunChangeRequest(
            provider=row["change_request_provider"],
            repository=row["change_request_repository"],
            external_id=row["change_request_external_id"],
        )
    return AFKRunSummary(
        afk_run_id=row["afk_run_id"],
        provider=row["provider"],
        status=row["status"],
        title=row["title"],
        repository=row["repository"],
        trigger_type=row["trigger_type"],
        recovered_from_afk_run_id=row["recovered_from_afk_run_id"],
        change_request=change_request,
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        outcome_status=row["outcome_status"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
    )


def _build_canonical_run_filters(
    provider: str | None,
    repository: str | None,
    status_filter: str | None,
    outcome_filter: str | None,
    has_change_request: bool | None,
    created_before: datetime | None,
) -> tuple[str, list[object]]:
    """Build a WHERE clause and parameter list for the canonical list query.

    * ``has_change_request`` (contract §4.1): ``True`` → bound
      (``change_request_provider IS NOT NULL``); ``False`` → unbound.
    * ``created_before``: ``afk_runs.first_seen_at < value`` (strict — the
      cutoff edge itself is retained).
    * ``repository``: matches the run's bound repository
      (``afk_runs.repository``) OR any linked entity's repository
      (``afk_run_entities.repository``) — reconstruction-era rows carry NULL
      repository but have entity links.
    """
    params: list[object] = []
    filters: list[str] = ["TRUE"]

    if provider is not None:
        filters.append(f"r.provider = ${len(params) + 1}")
        params.append(provider)

    if repository is not None:
        filters.append(
            f"(r.repository = ${len(params) + 1} OR EXISTS "
            f"(SELECT 1 FROM afk_run_entities re "
            f"WHERE re.afk_run_id = r.afk_run_id AND re.repository = ${len(params) + 1}))"
        )
        params.append(repository)

    if status_filter is not None:
        filters.append(f"r.status = ${len(params) + 1}")
        params.append(status_filter)
    if outcome_filter is not None:
        filters.append(f"r.outcome_status = ${len(params) + 1}")
        params.append(outcome_filter)
    if has_change_request is not None:
        filters.append(
            "r.change_request_provider IS NOT NULL"
            if has_change_request
            else "r.change_request_provider IS NULL"
        )
    if created_before is not None:
        filters.append(f"r.first_seen_at < ${len(params) + 1}")
        params.append(created_before)

    return " AND ".join(filters), params


# ── Query helpers ────────────────────────────────────────────────────────────


async def _fetch_canonical_runs(
    conn: asyncpg.Connection,
    provider: str | None,
    repository: str | None,
    status_filter: str | None,
    outcome_filter: str | None,
    has_change_request: bool | None,
    created_before: datetime | None,
    limit: int,
    offset: int,
    *,
    db_timeout_seconds: int,
) -> PaginatedResponse[AFKRunSummary]:
    """Execute count + data queries for the canonical list endpoint (§4.1)."""
    where_clause, params = _build_canonical_run_filters(
        provider,
        repository,
        status_filter,
        outcome_filter,
        has_change_request,
        created_before,
    )

    count_sql = f"SELECT COUNT(*) FROM afk_runs r WHERE {where_clause}"
    async with timed_operation("db.query.afk.canonical_runs.count", "db"):
        async with _db_timeout("db.query.afk.canonical_runs.count", db_timeout_seconds):
            total = await conn.fetchval(count_sql, *params)

    data_sql = f"""
        SELECT r.afk_run_id, r.provider, r.status, r.title, r.repository,
               r.trigger_type, r.recovered_from_afk_run_id,
               r.change_request_provider, r.change_request_repository,
               r.change_request_external_id,
               r.started_at, r.finished_at, r.outcome_status,
               r.first_seen_at, r.last_seen_at
        FROM afk_runs r
        WHERE {where_clause}
        ORDER BY r.last_seen_at DESC NULLS LAST, r.afk_run_id ASC
        LIMIT ${len(params) + 1}
        OFFSET ${len(params) + 2}
    """
    async with timed_operation("db.query.afk.canonical_runs.data", "db"):
        async with _db_timeout("db.query.afk.canonical_runs.data", db_timeout_seconds):
            rows = await conn.fetch(data_sql, *params, limit, offset)

    items = [_afk_run_summary(r) for r in rows]
    return PaginatedResponse(items=items, total=total, limit=limit, offset=offset)


async def _fetch_canonical_run_detail(
    conn: asyncpg.Connection,
    afk_run_id: str,
    *,
    db_timeout_seconds: int,
) -> AFKRunDetail | None:
    """Compose the full chain for one run (3 queries, no N+1) — contract §4.2.

    Reuses the outcomes run-detail composition with the run block replaced by
    the extended :class:`AFKRunSummary`.
    """
    async with timed_operation("db.query.afk.canonical_run.detail.run", "db"):
        async with _db_timeout(
            "db.query.afk.canonical_run.detail.run", db_timeout_seconds
        ):
            run_row = await conn.fetchrow(
                """
                SELECT afk_run_id, provider, status, title, started_at, finished_at,
                       outcome_status, outcome, first_seen_at, last_seen_at,
                       repository, trigger_type, recovered_from_afk_run_id,
                       change_request_provider, change_request_repository,
                       change_request_external_id
                FROM afk_runs
                WHERE afk_run_id = $1
                """,
                afk_run_id,
            )
    if run_row is None:
        return None

    async with timed_operation("db.query.afk.canonical_run.detail.entities", "db"):
        async with _db_timeout(
            "db.query.afk.canonical_run.detail.entities", db_timeout_seconds
        ):
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

    async with timed_operation("db.query.afk.canonical_run.detail.sessions", "db"):
        async with _db_timeout(
            "db.query.afk.canonical_run.detail.sessions", db_timeout_seconds
        ):
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

    detail = AFKRunDetail(
        run=_afk_run_summary(run_row),
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


async def _delete_orphan_run(
    conn: asyncpg.Connection,
    afk_run_id: str,
    *,
    db_timeout_seconds: int,
) -> None:
    """Delete one AFK Run if — and only if — it is an orphan (issue #674).

    Runs the whole check-then-delete sequence inside a single transaction so
    eligibility is evaluated under the run row's ``SELECT … FOR UPDATE`` lock:
    a concurrent execution-binding write blocks on the row lock until this
    transaction commits, then fails its FK check rather than corrupting the
    read-model.

    Orphan eligibility (all must hold):

    - zero ``execution_bindings`` rows referencing the run,
    - no bound change request (``change_request_provider IS NULL``),
    - zero ``delivery_log`` rows referencing the run.

    Only the run's own aggregate links are removed — ``afk_run_entities``,
    ``afk_run_sessions``, and ``unresolved_correlations`` — followed by the
    ``afk_runs`` row itself.  ``execution_bindings``, ``delivery_log``,
    ``sessions``, and ``engineering_events`` are never written by this path;
    the first two are exactly the tables the eligibility probes read.

    Raises:
        HTTPException(404): no run with this ``afk_run_id`` exists.
        HTTPException(409): the run fails one or more eligibility rules.
    """
    async with conn.transaction():
        async with timed_operation("db.delete.afk_run.lock", "db"):
            async with _db_timeout("db.delete.afk_run.lock", db_timeout_seconds):
                run_row = await conn.fetchrow(
                    """
                    SELECT change_request_provider
                    FROM afk_runs
                    WHERE afk_run_id = $1
                    FOR UPDATE
                    """,
                    afk_run_id,
                )
        if run_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"AFK run not found: {afk_run_id}",
            )

        blocked: list[str] = []
        if run_row["change_request_provider"] is not None:
            blocked.append("run has a bound change request")

        async with timed_operation("db.delete.afk_run.eligibility", "db"):
            async with _db_timeout(
                "db.delete.afk_run.eligibility", db_timeout_seconds
            ):
                has_bindings = await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM execution_bindings WHERE afk_run_id = $1
                    )
                    """,
                    afk_run_id,
                )
                if has_bindings:
                    blocked.append("run has execution bindings")

                has_deliveries = await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM delivery_log WHERE afk_run_id = $1
                    )
                    """,
                    afk_run_id,
                )
                if has_deliveries:
                    blocked.append("run has delivery log rows")

        if blocked:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"AFK run {afk_run_id} is not an orphan and cannot be "
                    f"deleted: {'; '.join(blocked)}"
                ),
            )

        async with timed_operation("db.delete.afk_run.links", "db"):
            async with _db_timeout("db.delete.afk_run.links", db_timeout_seconds):
                await conn.execute(
                    "DELETE FROM afk_run_entities WHERE afk_run_id = $1",
                    afk_run_id,
                )
                await conn.execute(
                    "DELETE FROM afk_run_sessions WHERE afk_run_id = $1",
                    afk_run_id,
                )
                await conn.execute(
                    "DELETE FROM unresolved_correlations WHERE afk_run_id = $1",
                    afk_run_id,
                )

        async with timed_operation("db.delete.afk_run.run", "db"):
            async with _db_timeout("db.delete.afk_run.run", db_timeout_seconds):
                await conn.execute(
                    "DELETE FROM afk_runs WHERE afk_run_id = $1",
                    afk_run_id,
                )


# ═════════════════════════════════════════════════════════════════════════
#  Endpoints
# ═════════════════════════════════════════════════════════════════════════


@router.get("/runs")
async def list_runs(
    request: Request,
    provider: str | None = Query(default=None),
    repository: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    outcome: str | None = Query(default=None),
    has_change_request: str | None = Query(default=None),
    created_before: str | None = Query(default=None),
    limit: str | None = Query(default=None),
    offset: str | None = Query(default=None),
    conn: asyncpg.Connection = Depends(get_session),
) -> PaginatedResponse[AFKRunSummary]:
    """List AFK Runs, paginated and filterable (canonical contract §4.1).

    Filters: ``provider``, ``repository`` (run-bound or entity-linked),
    ``status``, ``outcome``, ``has_change_request`` (boolean on the change-
    request binding), ``created_before`` (``first_seen_at`` strictly before
    the given timestamp).  Ordered by ``last_seen_at DESC NULLS LAST`` with a
    deterministic ``afk_run_id ASC`` tie-breaker.  Invalid enum values,
    unparseable datetimes, non-boolean ``has_change_request``, and
    out-of-range pagination raise 400.
    """
    _require_enum_value(provider, _VALID_PROVIDER, "provider")
    _require_enum_value(status_filter, _VALID_STATUS, "status")
    _require_enum_value(outcome, _VALID_OUTCOME, "outcome")
    has_change_request_bool = _parse_bool_param(has_change_request, "has_change_request")
    created_before_dt = _parse_datetime(created_before, "created_before")
    limit_value = _parse_limit(limit)
    offset_value = _parse_offset(offset)

    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        return await _fetch_canonical_runs(
            conn,
            provider,
            repository,
            status_filter,
            outcome,
            has_change_request_bool,
            created_before_dt,
            limit_value,
            offset_value,
            db_timeout_seconds=settings.database_timeout_seconds,
        )


@router.get("/runs/{afk_run_id}")
async def get_run_detail(
    request: Request,
    afk_run_id: str,
    conn: asyncpg.Connection = Depends(get_session),
) -> AFKRunDetail:
    """Return the canonical detail for one AFK Run (canonical contract §4.2).

    The full chain — outcome, engineering entities grouped by type (with
    correlation provenance and provisional markers), linked sessions, agents,
    and the run-level usage aggregate — with the run block carrying the
    extended :class:`AFKRunSummary`.  A non-ULID path id is rejected with 400
    before any database access; a well-formed unknown id is 404.
    """
    _validate_afk_run_id(afk_run_id)
    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        detail = await _fetch_canonical_run_detail(
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


@router.patch("/runs/{afk_run_id}", response_model=AFKRunSummary)
async def update_run(
    afk_run_id: str,
    body: AFKRunUpdateRequest,
    request: Request,
    auth: dict = Depends(require_awx_execution_binding_credential),
    conn: asyncpg.Connection = Depends(get_session),
) -> AFKRunSummary:
    """Apply a guarded update to one canonical AFK Run (issue #673).

    Mutable fields are exactly ``{title, status}`` — unknown fields, explicit
    nulls, and empty bodies are rejected with 422 (``extra="forbid"`` schema
    plus non-erasing update rules).  Lifecycle rules, evaluated by the
    repository under a ``SELECT ... FOR UPDATE`` row lock:

    * ``pending`` runs are provisional and patchable — every RunStatus
      transition (and title change) is applied.
    * Terminal runs (``completed`` / ``failed`` / ``cancelled`` /
      ``timed_out``) are frozen → 409 when the request would change them;
      history is never rewritten.
    * Identical values are an idempotent no-op → 200 with the unchanged
      summary.

    The response is the updated :class:`AFKRunSummary`.  Only the run's own
    ``title``/``status`` columns are written — linked execution bindings,
    change-request bindings, entity links, and session links are preserved
    by construction.  Auth = Admin API Key (global middleware) AND the
    dedicated ``awx-execution-bindings`` collector credential.
    """
    _validate_afk_run_id(afk_run_id)
    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        repo = AsyncpgOutcomeRepository(conn)
        async with timed_operation("db.update.afk_run.guarded", "db"):
            async with _db_timeout(
                "db.update.afk_run.guarded", settings.database_timeout_seconds
            ):
                result = await repo.update_afk_run(
                    afk_run_id=afk_run_id,
                    title=body.title,
                    title_provided="title" in body.model_fields_set,
                    status=body.status.value if body.status is not None else None,
                    status_provided="status" in body.model_fields_set,
                )

        if result.not_found:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"AFK run not found: {afk_run_id}",
            )
        if result.is_conflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "AFK run has reached a terminal status and is frozen: "
                    f"{afk_run_id}"
                ),
            )

        # Re-read the row for the response (the repository returns flags;
        # the caller re-reads — the established write-path pattern).
        async with timed_operation("db.query.afk.canonical_run.updated", "db"):
            async with _db_timeout(
                "db.query.afk.canonical_run.updated",
                settings.database_timeout_seconds,
            ):
                row = await conn.fetchrow(
                    """
                    SELECT afk_run_id, provider, status, title, repository,
                           trigger_type, recovered_from_afk_run_id,
                           change_request_provider, change_request_repository,
                           change_request_external_id,
                           started_at, finished_at, outcome_status,
                           first_seen_at, last_seen_at
                    FROM afk_runs
                    WHERE afk_run_id = $1
                    """,
                    afk_run_id,
                )
    if row is None:
        # Should not happen — the guarded update saw the row under lock.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve updated AFK run",
        )
    return _afk_run_summary(row)


@router.delete("/runs/{afk_run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_run(
    request: Request,
    afk_run_id: str,
    _operator_token: str = Depends(require_operator_token),
    conn: asyncpg.Connection = Depends(get_session),
) -> Response:
    """Delete an orphaned AFK Run (issue #674).

    Orphan-only: a run is eligible for deletion only when it has zero
    execution bindings, no bound change request, and zero delivery-log rows.
    Ineligible (linked or active) runs return 409 Conflict; an unknown
    ``afk_run_id`` returns 404 Not Found; a non-ULID path id is rejected
    with 400 before any database access.

    Authorization is two-layer: the global :class:`~app.core.auth.ApiKeyMiddleware`
    (Admin API Key) plus the dedicated :func:`~app.core.auth.require_operator_token`
    dependency — the Admin API Key alone does not satisfy the operator gate.

    Linked execution data is preserved: only the run row and its
    ``afk_run_entities`` / ``afk_run_sessions`` / ``unresolved_correlations``
    links are removed; ``execution_bindings``, ``delivery_log``, ``sessions``,
    and ``engineering_events`` are never touched.

    Returns 204 No Content on success.
    """
    _validate_afk_run_id(afk_run_id)
    settings = get_settings()
    async with _request_timeout(settings.total_request_timeout_seconds):
        await _delete_orphan_run(
            conn,
            afk_run_id,
            db_timeout_seconds=settings.database_timeout_seconds,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
