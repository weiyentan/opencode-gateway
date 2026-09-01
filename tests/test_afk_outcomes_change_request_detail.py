"""Tests for the change-request detail endpoint (issue #611).

``GET /api/v1/afk-outcomes/change-requests/{provider}/{repository}/{number}``
resolves one change request directly by its flattened stable resource
identity and returns one composite read model:

* **summary block** — provider state and AFK automation state as separate
  values, aggregate cost, merge/freshness enrichment, execution counts;
* **linked AFK runs** — with every durable link source;
* **ordered AWX execution bindings** — AWX job identity, outcome,
  timestamps, duration, failure metadata, purpose (when an explicit stored
  signal carries it), and per-execution session token/cost telemetry;
* **linked sessions + usage** — deduplicated, aggregated;
* **merge state** and the **optional provenance timeline**.

Missing optional identity/cost telemetry is ``null`` — never invented.
Unknown identities return the existing not-found contract; malformed
identities return validation errors.  Coverage: GitHub PRs, GitLab MRs,
repeated executions, missing data, and identity boundaries.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from tests.conftest import mock_row

_A_TS = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017
_B_TS = datetime(2026, 8, 5, 14, 30, 0, tzinfo=timezone.utc)  # noqa: UP017

_ENDPOINT = "/api/v1/afk-outcomes/change-requests/github/acme%2Fproj/42"
_GITLAB_ENDPOINT = "/api/v1/afk-outcomes/change-requests/gitlab/cloudnative-pg/6"


# ── Mock row builders ────────────────────────────────────────────────────────


def _mk_summary_row(
    *,
    provider: str = "github",
    repository: str = "acme/proj",
    external_id: str = "42",
    provider_state: str | None = "merged",
    automation_state: str | None = "completed",
    latest_activity_at: datetime | None = _B_TS,
    total_estimated_cost_usd: Decimal | None = Decimal("0.12"),
    merged_at: datetime | None = _B_TS,
    provider_state_observed_at: datetime | None = _B_TS,
    title: str | None = "Implement auth",
    execution_total: int = 2,
    execution_running: int = 0,
    execution_completed: int = 1,
    execution_failed: int = 1,
    execution_cancelled: int = 0,
):
    """Build a mock asyncpg row with the detail summary query's column shape."""
    return mock_row(
        {
            "provider": provider,
            "repository": repository,
            "external_id": external_id,
            "provider_state": provider_state,
            "automation_state": automation_state,
            "latest_activity_at": latest_activity_at,
            "total_estimated_cost_usd": total_estimated_cost_usd,
            "merged_at": merged_at,
            "provider_state_observed_at": provider_state_observed_at,
            "title": title,
            "execution_total": execution_total,
            "execution_running": execution_running,
            "execution_completed": execution_completed,
            "execution_failed": execution_failed,
            "execution_cancelled": execution_cancelled,
        }
    )


def _mk_run_row(
    *,
    afk_run_id: str = "01ARZ3NDEKTSV4RRFFQ69G5FAV",
    provider: str = "github",
    status: str = "completed",
    title: str | None = None,
    started_at: datetime | None = _A_TS,
    finished_at: datetime | None = _B_TS,
    outcome_status: str | None = "merged",
    first_seen_at: datetime | None = _A_TS,
    last_seen_at: datetime | None = _B_TS,
    link_sources: list[str] | None = None,
):
    return mock_row(
        {
            "afk_run_id": afk_run_id,
            "provider": provider,
            "status": status,
            "title": title,
            "started_at": started_at,
            "finished_at": finished_at,
            "outcome_status": outcome_status,
            "first_seen_at": first_seen_at,
            "last_seen_at": last_seen_at,
            "link_sources": link_sources or ["change_request_binding"],
        }
    )


def _mk_execution_row(
    *,
    awx_job_id: int = 1001,
    job_template_id: int = 42,
    external_session_id: str | None = "ses-dev-001",
    afk_run_id: str | None = "01ARZ3NDEKTSV4RRFFQ69G5FAV",
    outcome: str | None = "completed",
    purpose: str | None = None,
    trigger_type: str | None = "eda",
    source_event_id: str | None = "evt-1",
    branch: str | None = "feature/auth",
    title: str | None = "Implement auth module",
    started_at: datetime | None = _A_TS,
    finished_at: datetime | None = datetime(2026, 8, 1, 12, 40, 0, tzinfo=timezone.utc),  # noqa: UP017
    failure_reason: str | None = None,
    failure_summary: str | None = None,
    session_id: str | None = "11111111-1111-1111-1111-111111111111",
    total_input_tokens: int | None = 1000,
    total_output_tokens: int | None = 500,
    total_cache_read_tokens: int | None = 200,
    total_cache_write_tokens: int | None = 0,
    estimated_cost_usd: Decimal | None = Decimal("0.05"),
):
    return mock_row(
        {
            "awx_job_id": awx_job_id,
            "job_template_id": job_template_id,
            "external_session_id": external_session_id,
            "afk_run_id": afk_run_id,
            "outcome": outcome,
            "purpose": purpose,
            "trigger_type": trigger_type,
            "source_event_id": source_event_id,
            "branch": branch,
            "title": title,
            "started_at": started_at,
            "finished_at": finished_at,
            "failure_reason": failure_reason,
            "failure_summary": failure_summary,
            "session_id": session_id,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cache_read_tokens": total_cache_read_tokens,
            "total_cache_write_tokens": total_cache_write_tokens,
            "estimated_cost_usd": estimated_cost_usd,
        }
    )


def _mk_session_row(
    *,
    session_id: str | None = "11111111-1111-1111-1111-111111111111",
    external_session_id: str | None = "ses-dev-001",
    started_at: datetime | None = _A_TS,
    finished_at: datetime | None = _B_TS,
    agent: str | None = "code-editor",
    total_input_tokens: int = 1000,
    total_output_tokens: int = 500,
    total_cache_read_tokens: int = 200,
    total_cache_write_tokens: int = 0,
    total_estimated_cost_usd: Decimal | None = Decimal("0.12"),
    message_count: int = 12,
    parent_session_id: str | None = None,
):
    return mock_row(
        {
            "session_id": session_id,
            "external_session_id": external_session_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "agent": agent,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cache_read_tokens": total_cache_read_tokens,
            "total_cache_write_tokens": total_cache_write_tokens,
            "total_estimated_cost_usd": total_estimated_cost_usd,
            "message_count": message_count,
            "parent_session_id": parent_session_id,
        }
    )


def _mk_timeline_row(
    *,
    event_type: str = "change_request.opened",
    occurred_at: datetime = _A_TS,
    observed_via: str | None = "webhook",
    snapshot_at: datetime | None = _A_TS,
    actor: str | None = "dev",
):
    return mock_row(
        {
            "event_type": event_type,
            "occurred_at": occurred_at,
            "observed_via": observed_via,
            "snapshot_at": snapshot_at,
            "actor": actor,
        }
    )


def _wire(
    mock_conn: AsyncMock,
    *,
    exists: bool = True,
    summary=None,
    runs=None,
    executions=None,
    sessions=None,
    timeline=None,
) -> None:
    """Wire the mock connection for the detail query's six calls."""
    mock_conn.fetchval = AsyncMock(return_value=exists)
    mock_conn.fetchrow = AsyncMock(
        return_value=summary if summary is not None else _mk_summary_row()
    )
    mock_conn.fetch = AsyncMock(
        side_effect=[runs or [], executions or [], sessions or [], timeline or []]
    )


# ══════════════════════════════════════════════════════════════════════════
#  Authentication
# ══════════════════════════════════════════════════════════════════════════


class TestChangeRequestDetailAuth:
    """The endpoint requires API-key auth and returns the 401 envelope."""

    @pytest.mark.asyncio
    async def test_requires_auth(self):
        from app.core.factory import create_app

        app = create_app(configure_logging=False)
        app.state.pool = None
        transport = ASGITransport(app=app, raise_app_exceptions=False)

        async with AsyncClient(transport=transport, base_url="http://test") as c:
            response = await c.get(_ENDPOINT)

        assert response.status_code == 401
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "UNAUTHORIZED"


# ══════════════════════════════════════════════════════════════════════════
#  Detail composition
# ══════════════════════════════════════════════════════════════════════════


class TestChangeRequestDetail:
    """Tests for GET /change-requests/{provider}/{repository}/{number}."""

    @pytest.mark.asyncio
    async def test_github_pr_returns_full_detail(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        _wire(
            mock_conn,
            runs=[
                _mk_run_row(
                    link_sources=["change_request_binding", "execution"]
                )
            ],
            executions=[
                _mk_execution_row(),
                _mk_execution_row(
                    awx_job_id=1002,
                    outcome="failed",
                    failure_reason="lint failure",
                    failure_summary="redacted failure text",
                    started_at=None,
                    finished_at=None,
                    session_id=None,
                    external_session_id=None,
                    total_input_tokens=None,
                    total_output_tokens=None,
                    total_cache_read_tokens=None,
                    total_cache_write_tokens=None,
                    estimated_cost_usd=None,
                ),
            ],
            sessions=[_mk_session_row()],
            timeline=[
                _mk_timeline_row(),
                _mk_timeline_row(
                    event_type="change_request.merged", occurred_at=_B_TS
                ),
            ],
        )

        async with client as c:
            response = await c.get(_ENDPOINT)

        assert response.status_code == 200
        data = response.json()["data"]

        # Summary block — provider state and AFK automation state as
        # separate values, with merge/freshness enrichment.
        summary = data["change_request"]
        assert summary["provider"] == "github"
        assert summary["repository"] == "acme/proj"
        assert summary["external_id"] == "42"
        assert summary["resource_type"] == "change_request"
        assert summary["provider_state"] == "merged"
        assert summary["automation_state"] == "completed"
        assert summary["title"] == "Implement auth"
        assert summary["merged_at"] is not None
        assert summary["provider_state_observed_at"] is not None
        assert summary["executions"] == {
            "total": 2,
            "running": 0,
            "completed": 1,
            "failed": 1,
            "cancelled": 0,
        }
        assert Decimal(str(summary["total_estimated_cost_usd"])) == Decimal("0.12")

        # Linked AFK runs with every durable link source.
        assert len(data["afk_runs"]) == 1
        run = data["afk_runs"][0]
        assert run["afk_run_id"] == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
        assert run["status"] == "completed"
        assert run["link_sources"] == ["change_request_binding", "execution"]

        # Executions in deterministic order with AWX job identity, outcome,
        # timestamps, duration, and failure metadata where available.
        executions = data["executions"]
        assert len(executions) == 2
        first = executions[0]
        assert first["awx_job"]["job_id"] == "1001"
        assert first["awx_job"]["job_template_id"] == 42
        assert first["outcome"] == "completed"
        assert first["purpose"] is None  # no explicit signal — never invented
        assert first["trigger_type"] == "eda"
        assert first["duration_seconds"] == 2400.0
        assert first["estimated_cost_usd"] is not None
        assert first["total_input_tokens"] == 1000
        assert first["total_output_tokens"] == 500
        assert first["total_cache_read_tokens"] == 200
        second = executions[1]
        assert second["awx_job"]["job_id"] == "1002"
        assert second["outcome"] == "failed"
        assert second["failure_reason"] == "lint failure"
        assert second["failure_summary"] == "redacted failure text"
        assert second["duration_seconds"] is None

        # Sessions + usage aggregate.
        assert len(data["sessions"]) == 1
        assert data["sessions"][0]["external_session_id"] == "ses-dev-001"
        assert data["sessions"][0]["inferred"] is True
        assert data["usage"]["active_tokens"] == 1500  # 1000 + 500
        assert data["usage"]["cache_read_tokens"] == 200
        assert data["usage"]["session_count"] == 1
        assert Decimal(str(data["usage"]["estimated_cost_usd"])) == Decimal("0.12")

        # Aggregate cost (top-level, Gateway-owned) and merge state.
        assert Decimal(str(data["total_estimated_cost_usd"])) == Decimal("0.12")
        assert data["merge_state"]["state"] == "merged"
        assert data["merge_state"]["merged_at"] is not None

        # Optional provenance timeline (chronological facts).
        assert data["timeline"] is not None
        assert [e["event_type"] for e in data["timeline"]["events"]] == [
            "change_request.opened",
            "change_request.merged",
        ]

    @pytest.mark.asyncio
    async def test_gitlab_mr_open_automation_running_missing_cost(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Provider-state/AFK-state independence, unavailable cost, not-merged."""
        _wire(
            mock_conn,
            summary=_mk_summary_row(
                provider="gitlab",
                repository="cloudnative-pg/cloudnative-pg",
                external_id="6",
                provider_state="open",
                automation_state="running",
                merged_at=None,
                provider_state_observed_at=_A_TS,
                total_estimated_cost_usd=None,
                title=None,
                execution_total=1,
                execution_completed=0,
                execution_failed=0,
                execution_running=1,
            ),
            executions=[
                _mk_execution_row(
                    outcome="running",
                    started_at=_A_TS,
                    finished_at=None,
                    session_id=None,
                    external_session_id=None,
                    total_input_tokens=None,
                    total_output_tokens=None,
                    total_cache_read_tokens=None,
                    total_cache_write_tokens=None,
                    estimated_cost_usd=None,
                )
            ],
        )

        async with client as c:
            response = await c.get(_GITLAB_ENDPOINT)

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["change_request"]["provider"] == "gitlab"
        assert data["change_request"]["provider_state"] == "open"
        assert data["change_request"]["automation_state"] == "running"
        # Missing cost telemetry is null — never zero.
        assert data["change_request"]["total_estimated_cost_usd"] is None
        assert data["total_estimated_cost_usd"] is None
        assert data["usage"]["estimated_cost_usd"] is None
        # Facts observed but never merged -> not_merged.
        assert data["merge_state"]["state"] == "not_merged"
        assert data["merge_state"]["merged_at"] is None
        # No facts -> no timeline (null, not an empty list).
        assert data["timeline"] is None

    @pytest.mark.asyncio
    async def test_repeated_executions_preserved_in_order(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Failed + cancelled + completed attempts stay separate, earliest first."""
        _wire(
            mock_conn,
            executions=[
                _mk_execution_row(awx_job_id=1, outcome="failed"),
                _mk_execution_row(awx_job_id=2, outcome="cancelled"),
                _mk_execution_row(awx_job_id=3, outcome="completed"),
            ],
        )

        async with client as c:
            response = await c.get(_ENDPOINT)

        assert response.status_code == 200
        executions = response.json()["data"]["executions"]
        assert [e["awx_job"]["job_id"] for e in executions] == ["1", "2", "3"]
        assert [e["outcome"] for e in executions] == [
            "failed",
            "cancelled",
            "completed",
        ]

    @pytest.mark.asyncio
    async def test_missing_execution_telemetry_is_null_not_zero(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """A binding with no resolved session carries null telemetry."""
        _wire(
            mock_conn,
            summary=_mk_summary_row(
                total_estimated_cost_usd=None,
                merged_at=None,
                provider_state_observed_at=None,
                title=None,
                execution_total=1,
                execution_completed=1,
                execution_failed=0,
            ),
            executions=[
                _mk_execution_row(
                    session_id=None,
                    external_session_id=None,
                    total_input_tokens=None,
                    total_output_tokens=None,
                    total_cache_read_tokens=None,
                    total_cache_write_tokens=None,
                    estimated_cost_usd=None,
                )
            ],
        )

        async with client as c:
            response = await c.get(_ENDPOINT)

        data = response.json()["data"]
        execution = data["executions"][0]
        assert execution["session_id"] is None
        assert execution["total_input_tokens"] is None
        assert execution["total_output_tokens"] is None
        assert execution["total_cache_read_tokens"] is None
        assert execution["total_cache_write_tokens"] is None
        assert execution["estimated_cost_usd"] is None
        assert data["usage"]["active_tokens"] == 0
        assert data["usage"]["estimated_cost_usd"] is None

    @pytest.mark.asyncio
    async def test_retry_purpose_from_recovery_signal(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """An explicit recovery signal maps to purpose 'retry'; otherwise null."""
        _wire(
            mock_conn,
            executions=[
                _mk_execution_row(awx_job_id=1, purpose=None, trigger_type="eda"),
                _mk_execution_row(
                    awx_job_id=2, purpose="retry", trigger_type="recovery"
                ),
            ],
        )

        async with client as c:
            response = await c.get(_ENDPOINT)

        executions = response.json()["data"]["executions"]
        assert executions[0]["purpose"] is None
        assert executions[1]["purpose"] == "retry"

    @pytest.mark.asyncio
    async def test_template_id_purpose_params_reach_execution_query(
        self, client: AsyncClient, mock_conn: AsyncMock, monkeypatch
    ):
        """Configured implementation/review template IDs are passed to the
        execution SQL as $4 / $5 (the SQL classifies on them)."""
        from app.core.config import Settings

        _wire(mock_conn, executions=[_mk_execution_row()])

        monkeypatch.setattr(
            "app.api.afk_outcomes.get_settings",
            lambda: Settings(
                afk_implementation_job_template_ids="7, 42",
                afk_review_job_template_ids="99",
            ),
        )

        async with client as c:
            response = await c.get(_ENDPOINT)

        assert response.status_code == 200
        # The executions query is the third fetch (index 1 after runs at 0).
        executions_sql, provider, repository, number, impl_ids, review_ids = (
            mock_conn.fetch.call_args_list[1][0]
        )
        assert "job_template_id = ANY($4::bigint[])" in executions_sql
        assert provider == "github"
        assert repository == "acme/proj"
        assert number == "42"
        assert impl_ids == [7, 42]
        assert review_ids == [99]

    @pytest.mark.asyncio
    async def test_merge_state_none_when_no_facts(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """A change request known only from executions carries no merge state."""
        _wire(
            mock_conn,
            summary=_mk_summary_row(
                provider_state=None,
                automation_state=None,
                merged_at=None,
                provider_state_observed_at=None,
                title=None,
                total_estimated_cost_usd=None,
            ),
        )

        async with client as c:
            response = await c.get(_ENDPOINT)

        data = response.json()["data"]
        assert data["change_request"]["provider_state"] is None
        assert data["change_request"]["automation_state"] is None
        assert data["merge_state"] is None
        assert data["timeline"] is None

    @pytest.mark.asyncio
    async def test_deduplicates_linked_sessions_across_runs(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """The same session linked by two runs appears once in sessions/usage."""
        _wire(
            mock_conn,
            sessions=[
                _mk_session_row(),
                _mk_session_row(),  # duplicate across runs
            ],
        )

        async with client as c:
            response = await c.get(_ENDPOINT)

        data = response.json()["data"]
        assert len(data["sessions"]) == 1
        assert data["usage"]["session_count"] == 1
        assert data["usage"]["input_tokens"] == 1000  # not doubled


# ══════════════════════════════════════════════════════════════════════════
#  Not found and validation
# ══════════════════════════════════════════════════════════════════════════


class TestChangeRequestDetailErrors:
    @pytest.mark.asyncio
    async def test_unknown_identity_returns_404(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        _wire(mock_conn, exists=False)

        async with client as c:
            response = await c.get(_ENDPOINT)

        assert response.status_code == 404
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "NOT_FOUND"
        assert "not found" in payload["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_invalid_provider_returns_400(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        async with client as c:
            response = await c.get(
                "/api/v1/afk-outcomes/change-requests/bitbucket/acme/proj/42"
            )

        assert response.status_code == 400
        payload = response.json()
        assert payload["status"] == "error"
        assert payload["error"]["code"] == "BAD_REQUEST"

    @pytest.mark.asyncio
    async def test_blank_external_number_returns_400(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        async with client as c:
            response = await c.get(
                "/api/v1/afk-outcomes/change-requests/github/acme/proj/%20%20"
            )

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_blank_repository_returns_400(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        async with client as c:
            response = await c.get(
                "/api/v1/afk-outcomes/change-requests/github/%20/42"
            )

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_identity_boundaries_reach_query_verbatim(
        self, client: AsyncClient, mock_conn: AsyncMock
    ):
        """Path identity (provider, repository, number) is passed verbatim —
        the repository keeps its slash — with no normalization/reinvention."""
        _wire(mock_conn)

        async with client as c:
            response = await c.get(_ENDPOINT)

        assert response.status_code == 200
        # The existence check is the first query — verify its params.
        sql, provider, repository, number = mock_conn.fetchval.call_args[0]
        assert "FROM engineering_events" in sql
        assert provider == "github"
        assert repository == "acme/proj"
        assert number == "42"


# ══════════════════════════════════════════════════════════════════════════
#  Query builders / mappers
# ══════════════════════════════════════════════════════════════════════════


class TestChangeRequestDetailQueries:
    """Direct checks of the detail SQL and row mappers behind the endpoint."""

    def test_identity_universe_covers_three_sources(self):
        from app.api.afk_outcomes import _CHANGE_REQUEST_DETAIL_EXISTS_SQL

        sql = _CHANGE_REQUEST_DETAIL_EXISTS_SQL
        assert "FROM engineering_events" in sql
        assert "FROM afk_runs" in sql
        assert "FROM execution_bindings" in sql
        assert sql.count("UNION ALL") == 2

    def test_run_sources_cover_three_linkage_paths(self):
        from app.api.afk_outcomes import _CHANGE_REQUEST_DETAIL_RUN_SOURCES_BODY

        sql = _CHANGE_REQUEST_DETAIL_RUN_SOURCES_BODY
        assert "'change_request_binding'" in sql
        assert "'entity_link'" in sql
        assert "'execution'" in sql
        assert "FROM afk_run_entities" in sql

    def test_purpose_derived_from_recovery_and_template_signals(self):
        """Purpose classification: recovery → retry, configured templates →
        implementation / review, everything else NULL."""
        from app.api.afk_outcomes import _CHANGE_REQUEST_DETAIL_EXECUTIONS_SQL

        sql = _CHANGE_REQUEST_DETAIL_EXECUTIONS_SQL
        # Recovery signal → retry (first precedence).
        assert "WHEN eb.trigger_type = 'recovery'" in sql
        assert "OR r.trigger_type = 'recovery'" in sql
        assert "OR r.recovered_from_afk_run_id IS NOT NULL" in sql
        assert "THEN 'retry'" in sql
        # Configured AWX job-template sets classify implementation / review.
        assert "eb.job_template_id = ANY($4::bigint[])" in sql
        assert "THEN 'implementation'" in sql
        assert "eb.job_template_id = ANY($5::bigint[])" in sql
        assert "THEN 'review'" in sql
        # No signal → unavailable, never invented.
        assert "ELSE NULL" in sql

    def test_parse_job_template_ids(self):
        """Comma-separated template-ID settings parse to a list of ints."""
        from app.api.afk_outcomes import _parse_job_template_ids

        assert _parse_job_template_ids("") == []
        assert _parse_job_template_ids("  ") == []
        assert _parse_job_template_ids("7") == [7]
        assert _parse_job_template_ids("7, 42, 99") == [7, 42, 99]
        assert _parse_job_template_ids("7, , 42") == [7, 42]
        # Non-integer tokens are skipped, never fatal.
        assert _parse_job_template_ids("7, dev-loop, 42") == [7, 42]
        assert _parse_job_template_ids("dev-loop") == []

    def test_execution_ordering_is_deterministic_earliest_first(self):
        from app.api.afk_outcomes import _CHANGE_REQUEST_DETAIL_EXECUTIONS_SQL

        sql = _CHANGE_REQUEST_DETAIL_EXECUTIONS_SQL
        assert (
            "ORDER BY COALESCE(eb.started_at, eb.created_at) ASC, "
            "eb.awx_job_id ASC"
        ) in sql

    def test_executions_resolve_linked_runs_via_run_sources_cte(self):
        """Executions now resolve every linked AFK run through the shared
        three-source run_sources CTE (change_request_binding, entity_link,
        execution) and select bindings attached to those runs."""
        from app.api.afk_outcomes import _CHANGE_REQUEST_DETAIL_EXECUTIONS_SQL

        sql = _CHANGE_REQUEST_DETAIL_EXECUTIONS_SQL
        assert "WITH run_sources AS (" in sql
        assert "'change_request_binding'" in sql
        assert "'entity_link'" in sql
        assert "'execution'" in sql
        # The run-sources body is embedded (f-string interpolation), then the
        # matched-job set joins bindings on the resolved run IDs.
        assert "JOIN execution_bindings eb ON eb.afk_run_id = ri.afk_run_id" in sql

    def test_executions_include_direct_change_request_bindings(self):
        """Direct change-request bindings (even those not reachable through a
        run linkage path) are included in the matched job set."""
        from app.api.afk_outcomes import _CHANGE_REQUEST_DETAIL_EXECUTIONS_SQL

        sql = _CHANGE_REQUEST_DETAIL_EXECUTIONS_SQL
        assert "eb.entity_type = 'change_request'" in sql
        assert "eb.provider = $1" in sql
        assert "eb.repository_url = $2" in sql
        assert "eb.entity_number = $3" in sql

    def test_executions_deduplicate_by_awx_job_id(self):
        """The same AWX job reachable through several linkage paths appears
        exactly once: the matched set unions by awx_job_id and the final join
        back to execution_bindings is keyed on that unique job identity."""
        from app.api.afk_outcomes import _CHANGE_REQUEST_DETAIL_EXECUTIONS_SQL

        sql = _CHANGE_REQUEST_DETAIL_EXECUTIONS_SQL
        assert "UNION" in sql
        assert "JOIN execution_bindings eb ON eb.awx_job_id = m.awx_job_id" in sql

    def test_summary_state_precedence(self):
        from app.api.afk_outcomes import _CHANGE_REQUEST_DETAIL_SUMMARY_SQL

        sql = _CHANGE_REQUEST_DETAIL_SUMMARY_SQL
        # Provider state derives from the chronologically latest lifecycle
        # fact (a reopened PR/MR reports ``open`` again); the merged >
        # closed > open precedence survives only as the deterministic
        # tie-breaker for equal timestamps.
        assert "es.latest_merged_at" in sql
        assert "es.latest_closed_at" in sql
        assert "es.latest_opened_at" in sql
        assert "THEN 'merged'" in sql
        assert "THEN 'closed'" in sql
        assert "THEN 'open'" in sql
        assert "WHEN BOOL_OR(es.merged) THEN 'merged'" not in sql
        assert "WHEN BOOL_OR(es.closed) THEN 'closed'" not in sql
        assert "WHEN BOOL_OR(es.opened) THEN 'open'" not in sql
        # Success-aware automation precedence, mirroring #610's summary.
        assert "WHEN BOOL_OR(r.status = 'running') THEN 'running'" in sql
        assert "WHEN BOOL_OR(r.status = 'completed') THEN 'completed'" in sql
        assert "WHEN BOOL_OR(r.status = 'pending') THEN 'pending'" in sql

    def test_latest_activity_at_is_null_safe_greatest(self):
        from app.api.afk_outcomes import _CHANGE_REQUEST_DETAIL_SUMMARY_SQL

        sql = _CHANGE_REQUEST_DETAIL_SUMMARY_SQL
        assert "GREATEST(" in sql
        assert "MAX(r.last_seen_at)" in sql
        assert "MAX(es.latest_event_at)" in sql
        assert "MAX(ec.latest_exec_at)" in sql

    def test_cost_sum_never_coalesces_null_to_zero(self):
        from app.api.afk_outcomes import _CHANGE_REQUEST_DETAIL_SUMMARY_SQL

        sql = _CHANGE_REQUEST_DETAIL_SUMMARY_SQL
        # Missing cost telemetry must surface as SQL NULL (unavailable), so the
        # mapper yields None — the cost column derives from the deduplicated
        # ``session_cost`` aggregate, never wrapped in a COALESCE that would
        # rewrite missing telemetry to 0.
        assert "total_estimated_cost_usd" in sql
        assert "COALESCE(SUM(" not in sql
        assert "COALESCE(s.total_estimated_cost_usd" not in sql

    def test_cost_deduplicates_sessions_across_runs(self):
        from app.api.afk_outcomes import _CHANGE_REQUEST_DETAIL_SUMMARY_SQL

        sql = _CHANGE_REQUEST_DETAIL_SUMMARY_SQL
        # The same session linked to several AFK runs for one change request
        # (retries) must contribute its cost once — dedupe by internal session
        # UUID (``afk_run_sessions.session_id``) before SUMming.
        assert "SUM(s.total_estimated_cost_usd) AS total_estimated_cost_usd" not in sql
        assert "DISTINCT" in sql
        assert "session_id" in sql

    def test_timeline_ordered_by_occurrence_time(self):
        from app.api.afk_outcomes import _CHANGE_REQUEST_DETAIL_TIMELINE_SQL

        sql = _CHANGE_REQUEST_DETAIL_TIMELINE_SQL
        assert "ORDER BY occurred_at ASC" in sql

    def test_execution_mapper_computes_duration_and_preserves_purpose(self):
        from app.api.afk_outcomes import _change_request_execution_item

        item = _change_request_execution_item(_mk_execution_row(purpose="retry"))
        assert item.awx_job.job_id == "1001"
        assert item.awx_job.job_template_id == 42
        assert item.outcome == "completed"
        assert item.purpose == "retry"
        assert item.duration_seconds == 2400.0
        assert item.estimated_cost_usd == Decimal("0.05")

    def test_execution_mapper_missing_telemetry_is_none(self):
        from app.api.afk_outcomes import _change_request_execution_item

        item = _change_request_execution_item(
            _mk_execution_row(
                started_at=None,
                finished_at=None,
                session_id=None,
                total_input_tokens=None,
                total_cache_write_tokens=None,
                estimated_cost_usd=None,
                purpose=None,
            )
        )
        assert item.duration_seconds is None
        assert item.session_id is None
        assert item.total_input_tokens is None
        assert item.total_cache_write_tokens is None
        assert item.estimated_cost_usd is None
        assert item.purpose is None

    def test_summary_mapper_maps_enrichment_columns(self):
        from app.api.afk_outcomes import _change_request_detail_summary_row

        summary = _change_request_detail_summary_row(_mk_summary_row())
        assert summary.provider_state == "merged"
        assert summary.automation_state == "completed"
        assert summary.title == "Implement auth"
        assert summary.merged_at == _B_TS
        assert summary.provider_state_observed_at == _B_TS
        assert summary.total_estimated_cost_usd == Decimal("0.12")
        assert summary.executions.total == 2

    def test_run_mapper_maps_link_sources(self):
        from app.api.afk_outcomes import _change_request_linked_run_row

        run = _change_request_linked_run_row(
            _mk_run_row(link_sources=["entity_link", "execution"])
        )
        assert run.afk_run_id == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
        assert run.link_sources == ["entity_link", "execution"]

    def test_timeline_mapper_maps_fact_columns(self):
        from app.api.afk_outcomes import _change_request_timeline_event

        event = _change_request_timeline_event(_mk_timeline_row())
        assert event.event_type == "change_request.opened"
        assert event.occurred_at == _A_TS
        assert event.observed_via == "webhook"
        assert event.actor == "dev"
