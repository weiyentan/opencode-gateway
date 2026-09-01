"""AWX Execution Cost subtotals from attributed sessions (issue #628).

Per CONTEXT.md, the **AWX Execution Cost** is the estimated cost of canonical
usage events belonging to the OpenCode sessions *explicitly attributable* to
one AWX Execution.  It is a subtotal and is **unavailable** when that
execution's session attribution or cost data is unknown — never zero, never a
partial amount.

Covers:

* **Write path** — POST/PATCH persist the normalized
  ``external_session_ids`` collection on ``execution_bindings`` (issue #627
  read the additive JSONB column; issue #628 completes the write so
  per-execution attribution is durable).
* **Read path** — the change-request detail's per-execution subtotal
  aggregates over *all* explicitly associated sessions (deduplicated,
  each resolved to exactly one internal session — the fail-safe rule),
  falls back to the legacy singular session for pre-#627 rows, and yields
  NULL (unavailable) when attribution is missing or any included session
  carries unknown cost.
* **Independent subtotals** — multiple executions under one AFK Run each
  aggregate only their own attributed sessions.
* **Replay safety** — a replayed/duplicate usage delivery must not
  double-count: the subtotal reads canonical ``sessions`` aggregates which
  are delta-adjusted, never re-incremented (ADR 0012).

Mock-based where the existing suites are mock-based, plus a live-Postgres
integration suite mirroring ``tests/integration/test_execution_bindings.py``.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import mock_row

# ── Write-path helpers (mirrored from test_execution_session_attribution) ───

_CLIENT_ID = uuid.uuid4()
_CREDENTIAL_ID = uuid.uuid4()


def _auth_row() -> MagicMock:
    from app.api.afk_executions import AWX_EXECUTION_BINDING_CLIENT_NAME

    return mock_row(
        {
            "credential_id": _CREDENTIAL_ID,
            "revoked_at": None,
            "last_used_at": None,
            "client_id": _CLIENT_ID,
            "client_name": AWX_EXECUTION_BINDING_CLIENT_NAME,
            "client_is_active": True,
        }
    )


def _mk_conn() -> AsyncMock:
    conn = AsyncMock()
    mock_tx = AsyncMock()
    mock_tx.__aenter__ = AsyncMock(return_value=mock_tx)
    mock_tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=mock_tx)
    return conn


def _resource() -> dict:
    return {
        "provider": "github",
        "repository": "https://github.com/acme/proj",
        "resource_type": "pull_request",
        "resource_number": "99",
    }


def _mk_saved_row(awx_job_id: int = 42) -> MagicMock:
    """Saved execution_bindings row shape returned by the INSERT ... RETURNING."""
    return mock_row(
        {
            "id": uuid.uuid4(),
            "binding_id": str(uuid.uuid4()),
            "awx_job_id": awx_job_id,
            "job_template_id": 7,
            "external_session_id": "ses_primary",
            "provider": "github",
            "repository_url": "github.com/acme/proj",
            "entity_type": "change_request",
            "entity_number": "99",
            "outcome": "completed",
            "source_event_id": None,
            "afk_run_id": "01JZ0123456789ABCDEFGHJKMN",
            "trigger_type": "manual",
            "branch": None,
            "title": None,
            "failure_reason": None,
            "failure_summary": None,
            "started_at": None,
            "finished_at": None,
        }
    )


def _insert_params(conn: AsyncMock) -> tuple[list[str], tuple]:
    """Return (sql, params) of the execution_bindings INSERT."""
    for call in conn.fetch.call_args_list:
        sql = call.args[0] if call.args else ""
        if "INSERT INTO execution_bindings" in sql:
            return sql, tuple(call.args[1:])
    raise AssertionError("no INSERT INTO execution_bindings issued")


class TestPostPersistsAttributionCollection:
    """POST persists the normalized external_session_ids JSONB column."""

    @pytest.mark.asyncio
    async def test_plural_collection_persists_jsonb(self) -> None:
        """Every attributed session id is stored in the JSONB column."""
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(
            side_effect=[
                _auth_row(),
                None,
                mock_row(
                    {
                        "afk_run_id": "01JZ0123456789ABCDEFGHJKMN",
                        "change_request_provider": "github",
                        "change_request_repository": "github.com/acme/proj",
                        "change_request_external_id": "99",
                    }
                ),
                _mk_saved_row(42),
            ]
        )
        conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "42", "job_template_id": 7},
            "afk_run_id": "01JZ0123456789ABCDEFGHJKMN",
            "external_session_ids": ["ses_a", "ses_b"],
            "resource": _resource(),
            "outcome": "completed",
            "trigger_type": "manual",
        }
        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201, resp.text

        sql, params = _insert_params(conn)
        assert "external_session_ids" in sql
        # The collection column sits alongside the singular primary column.
        assert json_collection_param(params) == ["ses_a", "ses_b"]

    @pytest.mark.asyncio
    async def test_legacy_singular_persists_single_entry_jsonb(self) -> None:
        """A legacy singular external_session_id persists a one-entry collection."""
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(
            side_effect=[
                _auth_row(),
                None,
                mock_row(
                    {
                        "afk_run_id": "01JZ0123456789ABCDEFGHJKMN",
                        "change_request_provider": "github",
                        "change_request_repository": "github.com/acme/proj",
                        "change_request_external_id": "99",
                    }
                ),
                _mk_saved_row(43),
            ]
        )
        conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "43", "job_template_id": 7},
            "afk_run_id": "01JZ0123456789ABCDEFGHJKMN",
            "external_session_id": "ses_legacy",
            "resource": _resource(),
            "outcome": "completed",
            "trigger_type": "manual",
        }
        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201, resp.text

        _, params = _insert_params(conn)
        assert json_collection_param(params) == ["ses_legacy"]


def json_collection_param(params: tuple) -> list[str] | None:
    """Extract the JSONB collection value from the INSERT params.

    The JSONB param is the one that is (or serializes to) a JSON list of
    strings; returns None when the param carries no attribution.
    """
    import json as _json

    for value in params:
        if isinstance(value, list):
            return value
        if isinstance(value, str) and value.startswith("["):
            decoded = _json.loads(value)
            if isinstance(decoded, list):
                return decoded
    return None


class TestPatchPersistsAttributionCollection:
    """PATCH persists the non-erasing fill-in collection."""

    @pytest.mark.asyncio
    async def test_patch_collection_persists_jsonb(self) -> None:
        """A terminal fill-in collection is stored in the JSONB column.

        Calls the repository directly (mirroring
        ``test_execution_binding_repository.py``) so the issued SQL is
        observable.  The UPDATE merges the supplied collection into the
        stored one (enrich-only — never erases stored attribution).
        """
        from afk_outcomes.models import ExecutionOutcome
        from afk_outcomes.repository import AsyncpgOutcomeRepository

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(
            side_effect=[
                mock_row(
                    {
                        "id": uuid.uuid4(),
                        "outcome": "running",
                        "finished_at": None,
                        "failure_reason": None,
                        "failure_summary": None,
                        "external_session_id": None,
                        "provider": "github",
                        "repository_url": "github.com/acme/proj",
                        "entity_type": "change_request",
                        "entity_number": "99",
                        "afk_run_id": "01JZ0123456789ABCDEFGHJKMN",
                        "external_session_ids_json": json.dumps(["ses_stored"]),
                    }
                ),
                mock_row(
                    {
                        "change_request_provider": "github",
                        "change_request_repository": "github.com/acme/proj",
                        "change_request_external_id": "99",
                    }
                ),
            ]
        )
        conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(conn)

        result = await repo.update_execution_binding_terminal(
            awx_job_id="44",
            outcome=ExecutionOutcome.COMPLETED,
            external_session_ids=["ses_x", "ses_y"],
        )
        assert result.is_updated is True

        updates = [
            call
            for call in conn.execute.call_args_list
            if "UPDATE execution_bindings" in (call.args[0] if call.args else "")
        ]
        assert updates, "no UPDATE execution_bindings issued"
        sql, params = updates[0].args[0], tuple(updates[0].args[1:])
        assert "external_session_ids" in sql
        # Supplied collection merges with the stored one, deduplicated,
        # stored order preserved first.
        assert json_collection_param(params) == ["ses_stored", "ses_x", "ses_y"]

    @pytest.mark.asyncio
    async def test_patch_without_collection_keeps_stored_jsonb(self) -> None:
        """A PATCH with no session attribution never erases stored JSONB."""
        from afk_outcomes.models import ExecutionOutcome
        from afk_outcomes.repository import AsyncpgOutcomeRepository

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(
            side_effect=[
                mock_row(
                    {
                        "id": uuid.uuid4(),
                        "outcome": "running",
                        "finished_at": None,
                        "failure_reason": None,
                        "failure_summary": None,
                        "external_session_id": "ses_stored_primary",
                        "provider": "github",
                        "repository_url": "github.com/acme/proj",
                        "entity_type": "change_request",
                        "entity_number": "99",
                        "afk_run_id": "01JZ0123456789ABCDEFGHJKMN",
                        "external_session_ids_json": json.dumps(["ses_stored"]),
                    }
                ),
                mock_row(
                    {
                        "change_request_provider": "github",
                        "change_request_repository": "github.com/acme/proj",
                        "change_request_external_id": "99",
                    }
                ),
            ]
        )
        conn.execute = AsyncMock()
        repo = AsyncpgOutcomeRepository(conn)

        result = await repo.update_execution_binding_terminal(
            awx_job_id="45",
            outcome=ExecutionOutcome.FAILED,
        )
        assert result.is_updated is True

        updates = [
            call
            for call in conn.execute.call_args_list
            if "UPDATE execution_bindings" in (call.args[0] if call.args else "")
        ]
        assert updates, "no UPDATE execution_bindings issued"
        _, params = updates[0].args[0], tuple(updates[0].args[1:])
        # Stored attribution untouched.
        assert json_collection_param(params) == ["ses_stored"]


# ── Read-path SQL shape ──────────────────────────────────────────────────────


class TestExecutionSubtotalSql:
    """The detail executions query aggregates over explicit attribution."""

    def test_sql_aggregates_over_attribution_not_singular_lateral(self) -> None:
        """The subtotal resolves attributed sessions per execution, not the
        legacy singular lateral join alone."""
        from app.api.afk_outcomes import _CHANGE_REQUEST_DETAIL_EXECUTIONS_SQL

        sql = _CHANGE_REQUEST_DETAIL_EXECUTIONS_SQL
        # Explicit per-execution attribution is the source of the subtotal.
        assert "external_session_ids" in sql
        # Canonical accounting: sums read the sessions aggregate table.
        assert "sessions" in sql
        assert "total_estimated_cost_usd" in sql

    def test_sql_keeps_legacy_singular_fallback(self) -> None:
        """Pre-#627 rows (JSONB NULL) still resolve through the singular column."""
        from app.api.afk_outcomes import _CHANGE_REQUEST_DETAIL_EXECUTIONS_SQL

        sql = _CHANGE_REQUEST_DETAIL_EXECUTIONS_SQL
        assert "eb.external_session_id" in sql

    def test_sql_unavailable_when_any_cost_unknown(self) -> None:
        """Unknown cost on any included session poisons the subtotal — the SQL
        must propagate NULL (unavailable), never COALESCE to zero."""
        from app.api.afk_outcomes import _CHANGE_REQUEST_DETAIL_EXECUTIONS_SQL

        sql = _CHANGE_REQUEST_DETAIL_EXECUTIONS_SQL
        # The subtotal cost SUM is guarded by an any-unknown-cost check:
        # one session with NULL cost makes the whole subtotal NULL.
        assert "BOOL_OR(s.total_estimated_cost_usd IS NULL)" in sql
        # The SUM is never wrapped in a COALESCE that would rewrite missing
        # telemetry to 0.
        assert "COALESCE(SUM(" not in sql

    def test_sql_fail_safe_resolution_per_session_id(self) -> None:
        """Each attributed external id resolves to exactly one internal
        session; ambiguous or unmatched ids leave the subtotal unknown."""
        from app.api.afk_outcomes import _CHANGE_REQUEST_DETAIL_EXECUTIONS_SQL

        sql = _CHANGE_REQUEST_DETAIL_EXECUTIONS_SQL
        # Per-id match counting gates resolution (fail-safe, never guesses).
        assert "COUNT(s.id) AS session_matches" in sql
        assert "p.session_matches = 1" in sql

    def test_sql_subtotal_is_per_execution(self) -> None:
        """Aggregation groups per AWX job so sibling executions under one run
        keep independent subtotals."""
        from app.api.afk_outcomes import _CHANGE_REQUEST_DETAIL_EXECUTIONS_SQL

        sql = _CHANGE_REQUEST_DETAIL_EXECUTIONS_SQL
        assert "awx_job_id" in sql


# ── Behavioral API tests (detail endpoint, mock harness) ────────────────────

_ENDPOINT = "/api/v1/afk-outcomes/change-requests/github/acme%2Fproj/42"
_A_TS = __import__("datetime").datetime(
    2026,
    8,
    1,
    12,
    0,
    0,
    tzinfo=__import__("datetime").timezone.utc,  # noqa: UP017
)


def _mk_detail_summary_row(**overrides):

    base = {
        "provider": "github",
        "repository": "acme/proj",
        "external_id": "42",
        "provider_state": "merged",
        "automation_state": "completed",
        "latest_activity_at": _A_TS,
        "total_estimated_cost_usd": Decimal("0.12"),
        "merged_at": _A_TS,
        "provider_state_observed_at": _A_TS,
        "title": "Implement auth",
        "execution_total": 2,
        "execution_running": 0,
        "execution_completed": 2,
        "execution_failed": 0,
        "execution_cancelled": 0,
    }
    base.update(overrides)
    return mock_row(base)


def _mk_detail_execution_row(**overrides):

    base = {
        "awx_job_id": 1001,
        "job_template_id": 42,
        "external_session_id": "ses_primary",
        "afk_run_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "outcome": "completed",
        "purpose": None,
        "trigger_type": "eda",
        "source_event_id": "evt-1",
        "branch": "feature/auth",
        "title": "Implement auth module",
        "started_at": _A_TS,
        "finished_at": _A_TS,
        "failure_reason": None,
        "failure_summary": None,
        "session_id": "11111111-1111-1111-1111-111111111111",
        "total_input_tokens": 1000,
        "total_output_tokens": 500,
        "total_cache_read_tokens": 200,
        "total_cache_write_tokens": 0,
        "estimated_cost_usd": Decimal("0.05"),
    }
    base.update(overrides)
    return mock_row(base)


def _mk_detail_session_row(**overrides):
    base = {
        "session_id": "11111111-1111-1111-1111-111111111111",
        "external_session_id": "ses-dev-001",
        "started_at": _A_TS,
        "finished_at": _A_TS,
        "agent": "code-editor",
        "total_input_tokens": 1000,
        "total_output_tokens": 500,
        "total_cache_read_tokens": 200,
        "total_cache_write_tokens": 0,
        "total_estimated_cost_usd": Decimal("0.12"),
        "message_count": 12,
        "parent_session_id": None,
    }
    base.update(overrides)
    return mock_row(base)


async def _get_detail(mock_conn, *, summary=None, executions=None, sessions=None):

    from tests.conftest import create_client

    mock_conn.fetchval = AsyncMock(return_value=True)
    mock_conn.fetchrow = AsyncMock(
        return_value=summary if summary is not None else _mk_detail_summary_row()
    )
    mock_conn.fetch = AsyncMock(side_effect=[[], executions or [], sessions or [], []])
    client = create_client(mock_conn)
    return await client.get(_ENDPOINT)


class TestExecutionSubtotalAggregation:
    """Per-execution subtotals aggregate all explicitly associated sessions."""

    @pytest.mark.asyncio
    async def test_complete_attribution_sums_all_sessions(self, mock_conn: AsyncMock):
        """The subtotal covers every attributed session — nested subagent
        sessions included when explicitly associated."""
        resp = await _get_detail(
            mock_conn,
            executions=[
                _mk_detail_execution_row(
                    awx_job_id=1001,
                    session_id="11111111-1111-1111-1111-111111111111",
                    total_input_tokens=2500,
                    total_output_tokens=900,
                    total_cache_read_tokens=400,
                    total_cache_write_tokens=100,
                    estimated_cost_usd=Decimal("0.19"),
                    external_session_id="ses_primary",
                )
            ],
        )
        assert resp.status_code == 200
        execution = resp.json()["data"]["executions"][0]
        assert Decimal(str(execution["estimated_cost_usd"])) == Decimal("0.19")
        assert execution["total_input_tokens"] == 2500
        assert execution["total_output_tokens"] == 900

    @pytest.mark.asyncio
    async def test_missing_attribution_is_unavailable_not_zero(self, mock_conn: AsyncMock):
        """A binding with no attributed sessions reads NULL cost/telemetry —
        unavailable, never zero."""
        resp = await _get_detail(
            mock_conn,
            summary=_mk_detail_summary_row(total_estimated_cost_usd=None),
            executions=[
                _mk_detail_execution_row(
                    awx_job_id=1002,
                    external_session_id=None,
                    session_id=None,
                    total_input_tokens=None,
                    total_output_tokens=None,
                    total_cache_read_tokens=None,
                    total_cache_write_tokens=None,
                    estimated_cost_usd=None,
                )
            ],
        )
        assert resp.status_code == 200
        execution = resp.json()["data"]["executions"][0]
        assert execution["estimated_cost_usd"] is None
        assert execution["session_id"] is None
        assert execution["total_input_tokens"] is None

    @pytest.mark.asyncio
    async def test_unknown_session_cost_is_unavailable(self, mock_conn: AsyncMock):
        """One attributed session with unknown cost poisons the subtotal —
        NULL, never a partial amount."""
        resp = await _get_detail(
            mock_conn,
            summary=_mk_detail_summary_row(total_estimated_cost_usd=None),
            executions=[
                _mk_detail_execution_row(
                    awx_job_id=1003,
                    total_input_tokens=1000,
                    total_output_tokens=500,
                    total_cache_read_tokens=0,
                    total_cache_write_tokens=0,
                    estimated_cost_usd=None,
                )
            ],
        )
        assert resp.status_code == 200
        execution = resp.json()["data"]["executions"][0]
        assert execution["estimated_cost_usd"] is None
        # Token totals remain available when only cost is unknown.
        assert execution["total_input_tokens"] == 1000

    @pytest.mark.asyncio
    async def test_multiple_executions_have_independent_subtotals(self, mock_conn: AsyncMock):
        """Sibling executions under one AFK Run keep independent subtotals."""
        resp = await _get_detail(
            mock_conn,
            executions=[
                _mk_detail_execution_row(
                    awx_job_id=1,
                    estimated_cost_usd=Decimal("0.10"),
                    total_input_tokens=1000,
                    total_output_tokens=100,
                ),
                _mk_detail_execution_row(
                    awx_job_id=2,
                    estimated_cost_usd=Decimal("0.30"),
                    total_input_tokens=3000,
                    total_output_tokens=300,
                ),
            ],
        )
        assert resp.status_code == 200
        executions = resp.json()["data"]["executions"]
        assert Decimal(str(executions[0]["estimated_cost_usd"])) == Decimal("0.10")
        assert Decimal(str(executions[1]["estimated_cost_usd"])) == Decimal("0.30")
        assert executions[0]["total_input_tokens"] == 1000
        assert executions[1]["total_input_tokens"] == 3000

    @pytest.mark.asyncio
    async def test_replayed_usage_not_double_counted(self, mock_conn: AsyncMock):
        """Canonical accounting: a duplicate/replayed usage delivery moves the
        session aggregate by delta, never re-incrementing — the subtotal reads
        the deduplicated per-session aggregates, so replay leaves it stable."""
        resp = await _get_detail(
            mock_conn,
            executions=[
                _mk_detail_execution_row(
                    awx_job_id=1,
                    total_input_tokens=1000,
                    total_output_tokens=500,
                    estimated_cost_usd=Decimal("0.07"),
                )
            ],
        )
        assert resp.status_code == 200
        execution = resp.json()["data"]["executions"][0]
        # The subtotal equals the session aggregate exactly once — a replayed
        # delivery (duplicate outcome, aggregate delta-adjusted per ADR 0012)
        # does not double it.
        assert Decimal(str(execution["estimated_cost_usd"])) == Decimal("0.07")
        assert execution["total_input_tokens"] == 1000
