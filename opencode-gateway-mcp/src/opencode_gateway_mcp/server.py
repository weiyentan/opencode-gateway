"""MCP server bootstrap and the read-only semantic tools.

The adapter is deliberately read-only: it calls only published Gateway GET
endpoints — ``GET /health``, ``GET /api/v1/afk/dashboard/summary``,
``GET /api/v1/afk-outcomes/runs``, ``GET /api/v1/usage/aggregates``,
``GET /api/v1/afk-outcomes/runs/{afk_run_id}``,
``GET /api/v1/afk/executions/runs/{afk_run_id}``,
``GET /api/v1/afk-outcomes/change-requests/{provider}/{repository}/{external_number}``,
and ``GET /api/v1/afk-outcomes/correlations`` — and no write/admin or generic
passthrough capability is exposed. No per-AFK-run model attribution is performed
and candidates are never tie-broken.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, ValidationError

from opencode_gateway_mcp.client import (
    GatewayClient,
    GatewayConfig,
    GatewayConfigError,
    GatewayError,
)

SERVER_NAME = "opencode-gateway-mcp"
SERVER_VERSION = "0.1.0"

HEALTH_TOOL_DESCRIPTION = (
    "Return OpenCode Gateway service and ingestion health from GET /health: "
    "Gateway status/version, database connectivity, most recent ingest, remote "
    "collector health, and source-database health. Values (including "
    "healthy/stale/unknown and nulls) are preserved exactly as the Gateway reports them."
)

AFK_ACTIVITY_TOOL_DESCRIPTION = (
    "Return AFK rollup activity for an explicit date range via "
    "GET /api/v1/afk/dashboard/summary, optionally scoped to provider and "
    "repository. Supports interval daily/monthly. Rollup is UTC-calendar based "
    "(daily buckets or monthly sums of UTC daily buckets); no implicit today "
    "is substituted and no timezone-local reconstruction is claimed. Values "
    "including nulls and estimated cost are preserved as the Gateway reports them."
)

AFK_RUNS_TOOL_DESCRIPTION = (
    "Find AFK runs via GET /api/v1/afk-outcomes/runs using the Gateway's "
    "existing run filters. Supports repository, provider (translated to Gateway "
    "origin), outcome, explicit started/finished/seen time windows, and explicit "
    "limit/offset pagination without silent crawling. Unsupported filters are "
    "rejected rather than emulated. Null/unavailable values are preserved "
    "verbatim and Gateway 4xx/5xx are surfaced as MCP errors without exposing "
    "credentials."
)

MODEL_USAGE_TOOL_DESCRIPTION = (
    "Return usage aggregates grouped by model for an explicit date range from "
    "GET /api/v1/usage/aggregates?group_by=model. Requires start_date and end_date "
    "(ISO-8601). Optional Gateway-supported filters (client_id, model, session_id) "
    "are passed through only where the base API supports them. Preserves "
    "group_by=model semantics, session/record counts, token and cache fields, "
    "provider breakdown, and estimated cost with nulls preserved. No per-AFK-run "
    "model attribution is performed."
)

AGENT_USAGE_TOOL_DESCRIPTION = (
    "Return usage aggregates grouped by agent for an explicit date range from "
    "GET /api/v1/usage/aggregates?group_by=agent. Requires start_date and end_date "
    "(ISO-8601). Optional Gateway-supported filters (client_id, model, session_id) "
    "are passed through only where the base API supports them. Preserves "
    "group_by=agent semantics, session/record counts, token and cache fields, "
    "provider breakdown, and estimated cost with nulls preserved. No per-AFK-run "
    "model attribution is performed."
)

STORY_TOOL_DESCRIPTION = (
    "Return the complete AFK run story for one afk_run_id: the canonical run detail "
    "(issues, change requests, reviews, commits, merge events, agents, sessions, "
    "usage/cost) from GET /api/v1/afk-outcomes/runs/{afk_run_id} plus the full "
    "run-scoped AWX execution history including failed attempts and retries from "
    "GET /api/v1/afk/executions/runs/{afk_run_id} using the same explicit afk_run_id. "
    "Approved failure reason/summary metadata is preserved without raw prompts/stdout/secrets; "
    "no additional relationships are inferred; nulls and errors are preserved."
)

CHANGE_REQUEST_TOOL_DESCRIPTION = (
    "Return AFK evidence for one GitHub PR or GitLab MR from "
    "GET /api/v1/afk-outcomes/change-requests/{provider}/{repository}/{external_number}: "
    "linked AFK runs and AWX execution bindings/sessions with link provenance, "
    "provider lifecycle (open/closed/merged), merge time, timeline/provenance, "
    "usage and estimated cost where present. Null/unavailable values are preserved "
    "as null. Supports github and gitlab. No issue reverse lookup is performed."
)

CORRELATIONS_TOOL_DESCRIPTION = (
    "Return unresolved AFK correlation quality problems from "
    "GET /api/v1/afk-outcomes/correlations: unresolved correlation identity, "
    "AFK run identity, entity identity, reason (ambiguous or unmatched), "
    "candidates, and provenance. Supports optional reason filtering "
    "(ambiguous/unmatched) and explicit limit/offset pagination without "
    "silent crawl. Nulls are preserved and candidates are never tie-broken."
)

_VALID_PROVIDERS = frozenset({"github", "gitlab"})


class CollectorHealth(BaseModel):
    """One remote-collector client health row, passed through verbatim."""

    model_config = ConfigDict(extra="allow")

    client_id: str
    client_name: str
    last_heartbeat: str | None = None
    total_records_ingested: int = 0
    health: str


class SourceDatabaseHealth(BaseModel):
    """One source-database health row, passed through verbatim."""

    model_config = ConfigDict(extra="allow")

    source_database_id: str
    client_name: str
    last_push: str | None = None
    record_count: int = 0
    health: str


class GatewayHealth(BaseModel):
    """Structured Gateway health facts returned by ``get_gateway_health``.

    Fields mirror the published ``GET /health`` response. Timestamps stay
    strings so the Gateway's own representation is preserved, ``None`` stays
    ``None``, and ``extra="allow"`` keeps unknown future Gateway fields.
    """

    model_config = ConfigDict(extra="allow")

    status: str = "ok"
    version: str
    database: str = "disconnected"
    last_ingest_timestamp: str | None = None
    collectors: list[CollectorHealth] = []
    source_databases: list[SourceDatabaseHealth] = []


class AFKActivityBucket(BaseModel):
    """One UTC-calendar bucket from the AFK dashboard rollup.

    Fields preserve the Gateway's ``afk_dashboard_daily`` additive metrics
    verbatim. Strings stay strings so dates and ``derived_at`` timestamps are
    preserved exactly as the Gateway reports them (UTC-calendar). ``None``
    stays ``None`` and unknown future fields are allowed.
    """

    model_config = ConfigDict(extra="allow")

    period_start: str
    provider: str
    repository: str
    runs_started: int = 0
    change_requests_opened: int = 0
    change_requests_merged: int = 0
    change_requests_closed: int = 0
    execution_count: int = 0
    successful_execution_count: int = 0
    failed_execution_count: int = 0
    cancelled_execution_count: int = 0
    session_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    estimated_cost_usd: Any = None
    derived_at: str | None = None
    oldest_derived_at: str | None = None


class AFKActivitySummary(BaseModel):
    """Structured AFK dashboard summary returned by ``get_afk_activity_summary``.

    Mirrors ``GET /api/v1/afk/dashboard/summary`` with explicit UTC-calendar
    date range, optional provider/repository scoping, and daily/monthly
    interval. Values are preserved as reported; the rollup is UTC-calendar
    based with no implicit today.
    """

    model_config = ConfigDict(extra="allow")

    interval: str
    from_date: str
    to_date: str
    provider: str | None = None
    repository: str | None = None
    buckets: list[AFKActivityBucket] = []
    derived_at: str | None = None
    oldest_derived_at: str | None = None


class AFKRun(BaseModel):
    """One AFK run row, passed through verbatim from the Gateway.

    Timestamps are preserved as the Gateway's own string representation
    with ``None`` staying ``None``; ``extra="allow"`` keeps unknown future
    Gateway fields.
    """

    model_config = ConfigDict(extra="allow")

    afk_run_id: str
    provider: str | None = None
    title: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    outcome_status: str | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None


class ListAFKRunsResult(BaseModel):
    """Paginated AFK runs result returned by ``list_afk_runs``.

    Mirrors ``GET /api/v1/afk-outcomes/runs`` with explicit pagination and
    ``total``. Null/unavailable values are preserved without coercion.
    """

    model_config = ConfigDict(extra="allow")

    items: list[AFKRun] = []
    total: int = 0
    limit: int = 0
    offset: int = 0


class UsageAggregateRow(BaseModel):
    """One usage aggregate row preserved from GET /api/v1/usage/aggregates.

    Mirrors ``app.core.schemas.usage.AggregateRow`` but preserves nulls and
    unknown future fields via ``extra="allow"``. Token, cache, session/record,
    provider breakdown, and cost fields are returned exactly as the Gateway
    reports them; ``None`` stays ``None``.
    """

    model_config = ConfigDict(extra="allow")

    group_value: str
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cached_tokens: int = 0
    total_reasoning_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0
    total_estimated_cost_usd: Any = None
    record_count: int = 0
    session_count: int = 0
    model_count: int = 0
    cache_hit_ratio: float | None = None
    provider_breakdown: dict[str, int] = {}
    project_label: str | None = None
    agent: str | None = None


# ── AFK run story models ────────────────────────────────────────────────


class AfkRunSummary(BaseModel):
    """The run aggregate as reported by the canonical run-detail API."""

    model_config = ConfigDict(extra="allow")

    afk_run_id: str
    provider: str | None = None
    title: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    outcome_status: str | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None


class AfkUsageAggregate(BaseModel):
    """Per-run usage/cost aggregates preserving nulls and token vocabulary."""

    model_config = ConfigDict(extra="allow")

    active_tokens: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    estimated_cost_usd: float | str | None = None
    message_count: int | None = None
    session_count: int | None = None


class AfkExecutionBinding(BaseModel):
    """One AWX execution binding as reported by the run-scoped execution history.

    Approved failure metadata (failure_reason/failure_summary) is carried verbatim
    — bounded and redacted by the Gateway — without raw prompts, stdout, or secrets.
    ``extra="allow"`` preserves unknown future fields; nulls are preserved.
    """

    model_config = ConfigDict(extra="allow")

    binding_id: str | None = None
    awx_job: dict[str, Any] | None = None
    external_session_id: str | None = None
    external_session_ids: list[str] | None = None
    resource: dict[str, Any] | None = None
    outcome: str | None = None
    afk_run_id: str | None = None
    trigger_type: str | None = None
    source_event_id: str | None = None
    branch: str | None = None
    title: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    failure_reason: str | None = None
    failure_summary: str | None = None


class AfkRunStory(BaseModel):
    """Complete AFK run story combining canonical run-detail and execution history.

    All collections and scalars are preserved exactly as the Gateway reports them,
    including ``None``/``null``. No additional relationships are inferred by the
    adapter — the story is the mechanical composition of the two run-scoped APIs
    keyed by the same explicit ``afk_run_id``.
    """

    model_config = ConfigDict(extra="allow")

    afk_run_id: str
    run: AfkRunSummary | dict[str, Any] | None = None
    outcome: dict[str, Any] | None = None
    issues: list[dict[str, Any]] = []
    change_requests: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    commits: list[dict[str, Any]] = []
    merge_events: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    agents: list[str] = []
    usage: AfkUsageAggregate | dict[str, Any] | None = None
    executions: list[AfkExecutionBinding] = []


# ── Change-request story models (GET /api/v1/afk-outcomes/change-requests/...) ──


class ChangeRequestExecutionCounts(BaseModel):
    """Aggregated execution counts for one change request."""

    model_config = ConfigDict(extra="allow")

    total: int = 0
    running: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0


class ChangeRequestDetailSummary(BaseModel):
    """The change_request summary block inside the detail."""

    model_config = ConfigDict(extra="allow")

    provider: str
    repository: str
    external_id: str
    resource_type: str = "change_request"
    provider_state: str | None = None
    total_estimated_cost_usd: Any | None = None
    latest_linked_activity: str | None = None
    provider_state_observed_at: str | None = None
    merged_at: str | None = None
    title: str | None = None
    executions: ChangeRequestExecutionCounts | None = None


class ChangeRequestLinkedRun(BaseModel):
    """One AFK run linked to the change request, with link provenance."""

    model_config = ConfigDict(extra="allow")

    afk_run_id: str
    provider: str
    title: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    outcome_status: str | None = None
    first_seen_at: str | None = None
    last_seen_at: str | None = None
    link_sources: list[str] = []


class ChangeRequestExecutionItem(BaseModel):
    """One linked AWX execution binding with per-execution telemetry."""

    model_config = ConfigDict(extra="allow")

    awx_job: dict[str, Any]
    external_session_id: str | None = None
    session_id: str | None = None
    afk_run_id: str | None = None
    outcome: str | None = None
    purpose: str | None = None
    trigger_type: str | None = None
    source_event_id: str | None = None
    branch: str | None = None
    title: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    failure_reason: str | None = None
    failure_summary: str | None = None
    total_input_tokens: int | None = None
    total_output_tokens: int | None = None
    total_cache_read_tokens: int | None = None
    total_cache_write_tokens: int | None = None
    estimated_cost_usd: Any | None = None


class ChangeRequestSessionLink(BaseModel):
    """One linked session with telemetry; nulls preserved."""

    model_config = ConfigDict(extra="allow")

    session_id: str | None = None
    external_session_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    inferred: bool = True
    agent: str | None = None
    message_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cache_write_tokens: int = 0
    total_estimated_cost_usd: Any | None = None
    parent_session_id: str | None = None


class ChangeRequestUsageAggregate(BaseModel):
    """Aggregate usage/cost across linked sessions."""

    model_config = ConfigDict(extra="allow")

    active_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    estimated_cost_usd: Any | None = None
    message_count: int = 0
    session_count: int = 0


class ChangeRequestMergeState(BaseModel):
    """Provider merge state derived from observed facts."""

    model_config = ConfigDict(extra="allow")

    state: str
    merged_at: str | None = None


class ChangeRequestTimelineEvent(BaseModel):
    """One observed change_request fact in the provenance timeline."""

    model_config = ConfigDict(extra="allow")

    event_type: str
    occurred_at: str
    observed_via: str | None = None
    snapshot_at: str | None = None
    actor: str | None = None


class ChangeRequestTimeline(BaseModel):
    """Optional provenance timeline (chronologically ordered)."""

    model_config = ConfigDict(extra="allow")

    events: list[ChangeRequestTimelineEvent] = []


class ChangeRequestStory(BaseModel):
    """Structured AFK evidence for one GitHub PR or GitLab MR.

    Fields mirror the published GET /api/v1/afk-outcomes/change-requests/... response.
    Timestamps stay strings so the Gateway's own representation is preserved,
    ``None`` stays ``None``, and ``extra="allow"`` keeps unknown future fields.
    No issue reverse lookup or unsupported join is performed.
    """

    model_config = ConfigDict(extra="allow")

    change_request: ChangeRequestDetailSummary
    afk_runs: list[ChangeRequestLinkedRun] = []
    executions: list[ChangeRequestExecutionItem] = []
    sessions: list[ChangeRequestSessionLink] = []
    usage: ChangeRequestUsageAggregate = ChangeRequestUsageAggregate()
    total_estimated_cost_usd: Any | None = None
    merge_state: ChangeRequestMergeState | None = None
    timeline: ChangeRequestTimeline | None = None


# ── Correlation quality models ──────────────────────────────────────────


class CorrelationIssue(BaseModel):
    """One unresolved correlation row, passed through verbatim."""

    model_config = ConfigDict(extra="allow")

    entity_id: str
    entity_type: str
    external_id: str
    provider: str
    repository: str
    afk_run_id: str | None = None
    method: str
    reason: str | None = None
    correlation_confidence: float = 0.0
    candidates: list[str] = []
    evidence: list[Any] = []
    resolver_version: str | None = None
    created_at: str | None = None
    provisional: bool = True


class CorrelationIssuesResult(BaseModel):
    """Paginated unresolved correlation quality problems."""

    model_config = ConfigDict(extra="allow")

    items: list[CorrelationIssue]
    total: int
    limit: int
    offset: int


def create_server(
    config: GatewayConfig,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> MCPServer:
    """Create the MCP server with the read-only semantic tools registered.

    ``http_client`` lets callers (tests) supply a controlled HTTP transport;
    when omitted the client owns its own ``httpx.AsyncClient``.
    """
    gateway = GatewayClient(config, http_client=http_client)
    server: MCPServer = MCPServer(SERVER_NAME, version=SERVER_VERSION)

    @server.tool(description=HEALTH_TOOL_DESCRIPTION)  # type: ignore[untyped-decorator]
    async def get_gateway_health() -> GatewayHealth:
        try:
            payload: dict[str, Any] = await gateway.get_health()
        except GatewayError as exc:
            raise ToolError(str(exc)) from None
        try:
            return GatewayHealth.model_validate(payload)
        except ValidationError:
            raise ToolError(
                "OpenCode Gateway returned an unexpected /health payload shape"
            ) from None

    @server.tool(description=AFK_ACTIVITY_TOOL_DESCRIPTION)  # type: ignore[untyped-decorator]
    async def get_afk_activity_summary(
        from_date: str,
        to_date: str,
        provider: str | None = None,
        repository: str | None = None,
        interval: str = "daily",
    ) -> AFKActivitySummary:
        """Return AFK activity for an explicit UTC-calendar window.

        Calls only ``GET /api/v1/afk/dashboard/summary`` with the provided
        ``from_date``/``to_date``, optional ``provider``/``repository`` scoping,
        and ``interval`` (``daily`` or ``monthly``). No implicit today is
        substituted; UTC-calendar rollup semantics are preserved without
        timezone-local invention. Gateway 4xx/5xx and transport failures are
        surfaced as MCP-visible errors without exposing credentials.
        """
        try:
            payload: dict[str, Any] = await gateway.get_afk_dashboard_summary(
                from_date=from_date,
                to_date=to_date,
                provider=provider,
                repository=repository,
                interval=interval,
            )
        except GatewayError as exc:
            raise ToolError(str(exc)) from None
        try:
            return AFKActivitySummary.model_validate(payload)
        except ValidationError:
            raise ToolError(
                "OpenCode Gateway returned an unexpected "
                "/api/v1/afk/dashboard/summary payload shape"
            ) from None

    @server.tool(description=AFK_RUNS_TOOL_DESCRIPTION)  # type: ignore[untyped-decorator]
    async def list_afk_runs(
        repository: str | None = None,
        provider: str | None = None,
        outcome: str | None = None,
        started_from: str | None = None,
        started_to: str | None = None,
        finished_from: str | None = None,
        finished_to: str | None = None,
        seen_from: str | None = None,
        seen_to: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> ListAFKRunsResult:
        """Find AFK runs using the Gateway's existing run filters.

        Calls only ``GET /api/v1/afk-outcomes/runs`` via the authenticated
        Gateway HTTP client. ``provider`` is translated to the Gateway's
        ``origin`` filter. All supported filters, time windows, and explicit
        ``limit``/``offset`` pagination are forwarded verbatim without silent
        crawling. Unsupported filters are rejected (the tool schema only
        exposes supported filters). Gateway 4xx/5xx and transport failures are
        surfaced as MCP-visible errors without exposing credentials.
        """
        try:
            payload: dict[str, Any] = await gateway.list_afk_runs(
                repository=repository,
                provider=provider,
                outcome=outcome,
                started_from=started_from,
                started_to=started_to,
                finished_from=finished_from,
                finished_to=finished_to,
                seen_from=seen_from,
                seen_to=seen_to,
                limit=limit,
                offset=offset,
            )
        except GatewayError as exc:
            raise ToolError(str(exc)) from None
        try:
            return ListAFKRunsResult.model_validate(payload)
        except ValidationError:
            raise ToolError(
                "OpenCode Gateway returned an unexpected "
                "/api/v1/afk-outcomes/runs payload shape"
            ) from None

    @server.tool(description=MODEL_USAGE_TOOL_DESCRIPTION)  # type: ignore[untyped-decorator]
    async def get_model_usage(
        start_date: str,
        end_date: str,
        client_id: str | None = None,
        model: str | None = None,
        session_id: str | None = None,
    ) -> list[UsageAggregateRow]:
        """Return usage aggregates grouped by model for the explicit date range."""
        try:
            rows = await gateway.get_usage_aggregates(
                start_date=start_date,
                end_date=end_date,
                group_by="model",
                client_id=client_id,
                model=model,
                session_id=session_id,
            )
        except GatewayError as exc:
            raise ToolError(str(exc)) from None
        try:
            return [UsageAggregateRow.model_validate(r) for r in rows]
        except ValidationError:
            raise ToolError(
                "OpenCode Gateway returned an unexpected /api/v1/usage/aggregates payload shape"
            ) from None

    @server.tool(description=AGENT_USAGE_TOOL_DESCRIPTION)  # type: ignore[untyped-decorator]
    async def get_agent_usage(
        start_date: str,
        end_date: str,
        client_id: str | None = None,
        model: str | None = None,
        session_id: str | None = None,
    ) -> list[UsageAggregateRow]:
        """Return usage aggregates grouped by agent for the explicit date range."""
        try:
            rows = await gateway.get_usage_aggregates(
                start_date=start_date,
                end_date=end_date,
                group_by="agent",
                client_id=client_id,
                model=model,
                session_id=session_id,
            )
        except GatewayError as exc:
            raise ToolError(str(exc)) from None
        try:
            return [UsageAggregateRow.model_validate(r) for r in rows]
        except ValidationError:
            raise ToolError(
                "OpenCode Gateway returned an unexpected /api/v1/usage/aggregates payload shape"
            ) from None

    @server.tool(description=STORY_TOOL_DESCRIPTION)  # type: ignore[untyped-decorator]
    async def get_afk_run_story(afk_run_id: str) -> AfkRunStory:
        """Return the complete AFK run story for one ``afk_run_id``.

        Composition rule is mechanical: the same explicit ``afk_run_id`` is used
        for both ``GET /api/v1/afk-outcomes/runs/{afk_run_id}`` (canonical
        run-detail) and ``GET /api/v1/afk/executions/runs/{afk_run_id}``
        (run-scoped AWX execution history). No additional relationships are
        invented. A failure in either required call is surfaced as an
        MCP-visible error rather than a partial success. ``null`` values are
        preserved.
        """
        if not isinstance(afk_run_id, str) or not afk_run_id.strip():
            raise ToolError("afk_run_id must be a non-empty string")
        trimmed = afk_run_id.strip()
        # The two required calls are issued concurrently; a failure in either
        # one is surfaced as a single MCP-visible error, never a partial success.
        try:
            detail, executions_raw = await asyncio.gather(
                gateway.get_afk_run_detail(trimmed),
                gateway.get_afk_executions_for_run(trimmed),
            )
        except GatewayError as exc:
            raise ToolError(str(exc)) from None
        try:
            executions = [AfkExecutionBinding.model_validate(item) for item in executions_raw]
        except ValidationError:
            raise ToolError(
                "OpenCode Gateway returned an unexpected "
                "/api/v1/afk/executions/runs/{afk_run_id} payload shape"
            ) from None
        story_payload: dict[str, Any] = {
            "afk_run_id": trimmed,
            "run": detail.get("run"),
            "outcome": detail.get("outcome"),
            "issues": detail.get("issues", []),
            "change_requests": detail.get("change_requests", []),
            "reviews": detail.get("reviews", []),
            "commits": detail.get("commits", []),
            "merge_events": detail.get("merge_events", []),
            "sessions": detail.get("sessions", []),
            "agents": detail.get("agents", []),
            "usage": detail.get("usage"),
            "executions": [e.model_dump(mode="json", exclude_none=False) for e in executions],
        }
        try:
            return AfkRunStory.model_validate(story_payload)
        except ValidationError:
            raise ToolError(
                "OpenCode Gateway returned an unexpected afk run story payload shape"
            ) from None

    @server.tool(description=CHANGE_REQUEST_TOOL_DESCRIPTION)  # type: ignore[untyped-decorator]
    async def get_change_request_story(
        provider: str,
        repository: str,
        external_number: str,
    ) -> ChangeRequestStory:
        if provider not in _VALID_PROVIDERS:
            valid = ", ".join(sorted(_VALID_PROVIDERS))
            raise ToolError(f"Invalid provider: {provider!r}. Valid values: {valid}")
        if not repository or not repository.strip():
            raise ToolError(
                "Invalid change-request identity: repository must be a non-empty string"
            )
        if not external_number or not external_number.strip():
            raise ToolError(
                "Invalid change-request identity: external number must be a non-empty string"
            )
        try:
            payload: dict[str, Any] = await gateway.get_change_request_detail(
                provider.strip(), repository.strip(), external_number.strip()
            )
        except GatewayError as exc:
            raise ToolError(str(exc)) from None
        try:
            return ChangeRequestStory.model_validate(payload)
        except ValidationError:
            raise ToolError(
                "OpenCode Gateway returned an unexpected change-request payload shape"
            ) from None

    @server.tool(description=CORRELATIONS_TOOL_DESCRIPTION)  # type: ignore[untyped-decorator]
    async def get_correlation_issues(
        reason: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> CorrelationIssuesResult:
        try:
            payload: dict[str, Any] = await gateway.get_correlation_issues(
                reason=reason, limit=limit, offset=offset
            )
        except GatewayError as exc:
            raise ToolError(str(exc)) from None
        try:
            return CorrelationIssuesResult.model_validate(payload)
        except ValidationError:
            raise ToolError(
                "OpenCode Gateway returned an unexpected correlations payload shape"
            ) from None

    return server


def main() -> None:
    """Run the MCP server over stdio using environment configuration."""
    try:
        config = GatewayConfig.from_env()
    except GatewayConfigError as exc:
        # stderr only: stdout is the MCP stdio protocol channel.
        print(f"{SERVER_NAME}: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    create_server(config).run()


if __name__ == "__main__":
    main()
