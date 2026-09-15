"""End-to-end and regression coverage for the canonical AFK Run API (#675).

The canonical router (``app/api/afk_runs.py``, mounted at ``/api/v1/afk``)
answers the resource-oriented AFK Run surface:

- ``GET    /api/v1/afk/runs``          — paginated, filterable list
- ``GET    /api/v1/afk/runs/{id}``     — full-chain detail
- ``PATCH  /api/v1/afk/runs/{id}``     — guarded lifecycle update (issue #673)
- ``DELETE /api/v1/afk/runs/{id}``     — orphan-only deletion (issue #674)

These tests drive the API through the public HTTP surface with the shared
mock-DB client (``tests.conftest``), so they assert observable behaviour — the
``{status, data, error}`` envelope, status codes, pagination, filters,
authorization gates, orphan eligibility, and deletion scope — rather than the
internal SQL.  They deliberately live beside the existing per-endpoint suites
(``tests/test_api_afk_runs.py``) to add the cross-endpoint lifecycle flows,
the operator-gate fail-closed path, and the compatibility regression coverage
for the execution-scoped API that must remain unchanged (contract §6).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import create_client, mock_row

# ── Fixtures and constants ──────────────────────────────────────────────────

_RUN_ID = "01J8ABCDEFGHJKMNPQRSTVWXYZ"
_A_TS = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017
_B_TS = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)  # noqa: UP017
_CUT_TS = datetime(2026, 8, 1, 0, 0, 0, tzinfo=timezone.utc)  # noqa: UP017

_SESSION_ID = uuid.uuid4()

# Sentinel so callers can request an explicit ``outcome=None``.
_DEFAULT_OUTCOME = object()


# ── Mock row builders ───────────────────────────────────────────────────────


def _mk_run_row(
    *,
    afk_run_id: str = _RUN_ID,
    provider: str = "github",
    status: str = "running",
    title: str | None = "Fix login bug",
    started_at: datetime | None = _A_TS,
    finished_at: datetime | None = None,
    outcome_status: str | None = None,
    outcome: object = None,
    first_seen_at: datetime | None = _A_TS,
    last_seen_at: datetime | None = _B_TS,
    repository: str | None = None,
    trigger_type: str | None = None,
    change_request_provider: str | None = None,
    change_request_repository: str | None = None,
    change_request_external_id: str | None = None,
    recovered_from_afk_run_id: str | None = None,
):
    """Build a mock ``afk_runs`` row covering every column the API reads."""
    return mock_row(
        {
            "afk_run_id": afk_run_id,
            "provider": provider,
            "status": status,
            "title": title,
            "started_at": started_at,
            "finished_at": finished_at,
            "outcome_status": outcome_status,
            "outcome": outcome if outcome is not _DEFAULT_OUTCOME else None,
            "first_seen_at": first_seen_at,
            "last_seen_at": last_seen_at,
            "repository": repository,
            "trigger_type": trigger_type,
            "change_request_provider": change_request_provider,
            "change_request_repository": change_request_repository,
            "change_request_external_id": change_request_external_id,
            "recovered_from_afk_run_id": recovered_from_afk_run_id,
        }
    )


def _mk_entity_row(
    *,
    provider: str = "github",
    repository: str = "acme/proj",
    entity_type: str = "issue",
    external_id: str = "37",
    role: str = "resolved",
    correlation_method: str | None = "issue_reference",
    correlation_confidence: float = 1.0,
    evidence: list | None = None,
    resolver_version: str | None = "2",
    owning_change_request_id: str | None = None,
    correlation_source: str = "direct",
):
    """Build a mock ``afk_run_entities`` row for the detail composition."""
    return mock_row(
        {
            "provider": provider,
            "repository": repository,
            "entity_type": entity_type,
            "external_id": external_id,
            "role": role,
            "correlation_method": correlation_method,
            "correlation_confidence": correlation_confidence,
            "evidence": evidence
            if evidence is not None
            else [
                {
                    "kind": "issue_reference",
                    "source_entity_id": "change_request:42",
                    "detail": "resolves #37",
                    "weight": 1.0,
                }
            ],
            "resolver_version": resolver_version,
            "owning_change_request_id": owning_change_request_id,
            "correlation_source": correlation_source,
        }
    )


def _mk_session_row(
    *,
    session_id: uuid.UUID | None = _SESSION_ID,
    external_session_id: str | None = "ses_abc123",
    agent: str | None = "code-editor",
    total_input_tokens: int = 500,
    total_output_tokens: int = 250,
    total_cache_read_tokens: int = 100,
    total_cache_write_tokens: int = 50,
    total_estimated_cost_usd: Decimal | None = Decimal("0.0175"),
    message_count: int = 5,
):
    """Build a mock ``afk_run_sessions`` joined session row."""
    return mock_row(
        {
            "session_id": session_id,
            "external_session_id": external_session_id,
            "parent_session_id": None,
            "started_at": _A_TS,
            "finished_at": _B_TS,
            "agent": agent,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_cache_read_tokens": total_cache_read_tokens,
            "total_cache_write_tokens": total_cache_write_tokens,
            "total_estimated_cost_usd": total_estimated_cost_usd,
            "message_count": message_count,
        }
    )


def _credential_auth_row() -> MagicMock:
    """Mock row passing ``require_awx_execution_binding_credential``."""
    from app.api.afk_executions import AWX_EXECUTION_BINDING_CLIENT_NAME

    return mock_row(
        {
            "credential_id": uuid.uuid4(),
            "revoked_at": None,
            "last_used_at": None,
            "client_id": uuid.uuid4(),
            "client_name": AWX_EXECUTION_BINDING_CLIENT_NAME,
            "client_is_active": True,
        }
    )


def _mk_conn() -> AsyncMock:
    """Mock asyncpg connection with a working async transaction context."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchval = AsyncMock(return_value=0)
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock()
    mock_tx = AsyncMock()
    mock_tx.__aenter__ = AsyncMock(return_value=mock_tx)
    mock_tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=mock_tx)
    return conn


# ══════════════════════════════════════════════════════════════════════════
#  End-to-end lifecycle: list → detail → update → delete
# ══════════════════════════════════════════════════════════════════════════


class TestEndToEndLifecycle:
    """One AFK Run is driven through every canonical flow in sequence."""

    @pytest.mark.asyncio
    async def test_list_detail_update_delete_flow(self) -> None:
        conn = _mk_conn()
        running_row = _mk_run_row(status="running", title="Fix login bug")
        guarded_row = _mk_run_row(status="running", title="Fix login bug")
        updated_row = _mk_run_row(status="completed", title="Fix login bug")
        orphan_row = _mk_run_row(status="completed", title="Fix login bug")

        # GET /runs:            fetchval(count) → fetch(data)
        # GET /runs/{id}:       fetchrow(run) → fetch(entities) → fetch(sessions)
        # PATCH /runs/{id}:     fetchrow(credential) → [touch] → fetchrow(guarded)
        #                       → [UPDATE] → fetchrow(re-read)
        # DELETE /runs/{id}:    fetchrow(FOR UPDATE) → fetchval ×2 → execute ×4
        conn.fetchval = AsyncMock(side_effect=[1, False, False])
        conn.fetch = AsyncMock(side_effect=[[running_row], [], []])
        conn.fetchrow = AsyncMock(
            side_effect=[
                running_row,
                _credential_auth_row(),
                guarded_row,
                updated_row,
                orphan_row,
            ]
        )
        conn.execute = AsyncMock(
            side_effect=["UPDATE 1", "UPDATE 1", "DELETE 0", "DELETE 0", "DELETE 0", "DELETE 1"]
        )
        client = create_client(conn)

        async with client as c:
            listed = await c.get("/api/v1/afk/runs")
            assert listed.status_code == 200
            body = listed.json()
            assert body["status"] == "ok"
            assert body["data"]["total"] == 1
            assert body["data"]["items"][0]["status"] == "running"

            detail = await c.get(f"/api/v1/afk/runs/{_RUN_ID}")
            assert detail.status_code == 200
            assert detail.json()["data"]["run"]["afk_run_id"] == _RUN_ID

            patched = await c.patch(
                f"/api/v1/afk/runs/{_RUN_ID}",
                json={"title": "Fix login bug", "status": "completed"},
            )
            assert patched.status_code == 200
            assert patched.json()["data"]["status"] == "completed"

            deleted = await c.delete(f"/api/v1/afk/runs/{_RUN_ID}")
            assert deleted.status_code == 204
            assert deleted.content == b""

        # The delete flow ran inside a transaction that locked the row.
        assert conn.transaction.call_count == 2
        lock_sql = conn.fetchrow.call_args_list[4][0][0]
        assert "FOR UPDATE" in lock_sql

    @pytest.mark.asyncio
    async def test_detail_returns_full_chain(self) -> None:
        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_mk_run_row(status="completed"))
        conn.fetch = AsyncMock(
            side_effect=[
                [
                    _mk_entity_row(entity_type="issue", external_id="37"),
                    _mk_entity_row(entity_type="change_request", external_id="42"),
                ],
                [_mk_session_row()],
            ]
        )
        client = create_client(conn)

        async with client as c:
            response = await c.get(f"/api/v1/afk/runs/{_RUN_ID}")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["run"]["afk_run_id"] == _RUN_ID
        assert [i["entity_id"] for i in data["issues"]] == ["issue:37"]
        assert [cr["entity_id"] for cr in data["change_requests"]] == ["change_request:42"]
        assert data["agents"] == ["code-editor"]
        assert data["usage"]["active_tokens"] == 750
        assert data["usage"]["cache_read_tokens"] == 100
        assert data["sessions"][0]["inferred"] is True


# ══════════════════════════════════════════════════════════════════════════
#  Pagination
# ══════════════════════════════════════════════════════════════════════════


class TestPagination:
    """``limit`` / ``offset`` are validated, echoed, and passed through."""

    @pytest.mark.asyncio
    async def test_pagination_metadata_and_query_bounds(self) -> None:
        conn = _mk_conn()
        conn.fetchval = AsyncMock(return_value=137)
        conn.fetch = AsyncMock(return_value=[_mk_run_row()])
        client = create_client(conn)

        async with client as c:
            response = await c.get("/api/v1/afk/runs", params={"limit": "10", "offset": "20"})

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 137
        assert data["limit"] == 10
        assert data["offset"] == 20
        assert conn.fetch.call_args[0][-2:] == (10, 20)

    @pytest.mark.asyncio
    async def test_default_pagination_is_50_0(self) -> None:
        conn = _mk_conn()
        conn.fetchval = AsyncMock(return_value=0)
        conn.fetch = AsyncMock(return_value=[])
        client = create_client(conn)

        async with client as c:
            response = await c.get("/api/v1/afk/runs")

        data = response.json()["data"]
        assert data["limit"] == 50
        assert data["offset"] == 0
        assert conn.fetch.call_args[0][-2:] == (50, 0)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw_limit", ["1", "1000"])
    async def test_limit_boundaries_accepted(self, raw_limit: str) -> None:
        conn = _mk_conn()
        conn.fetchval = AsyncMock(return_value=0)
        conn.fetch = AsyncMock(return_value=[])
        client = create_client(conn)

        async with client as c:
            response = await c.get("/api/v1/afk/runs", params={"limit": raw_limit})

        assert response.status_code == 200
        assert response.json()["data"]["limit"] == int(raw_limit)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", ["0", "1001", "-5", "ten"])
    async def test_invalid_limit_returns_400(self, raw: str) -> None:
        conn = _mk_conn()
        client = create_client(conn)

        async with client as c:
            response = await c.get("/api/v1/afk/runs", params={"limit": raw})

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "BAD_REQUEST"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", ["-1", "later"])
    async def test_invalid_offset_returns_400(self, raw: str) -> None:
        conn = _mk_conn()
        client = create_client(conn)

        async with client as c:
            response = await c.get("/api/v1/afk/runs", params={"offset": raw})

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "BAD_REQUEST"


# ══════════════════════════════════════════════════════════════════════════
#  Filters
# ══════════════════════════════════════════════════════════════════════════


class TestFilters:
    """Every documented filter narrows the query without leaking state."""

    @pytest.mark.asyncio
    async def test_all_filters_parameterised_in_order(self) -> None:
        conn = _mk_conn()
        conn.fetchval = AsyncMock(return_value=0)
        conn.fetch = AsyncMock(return_value=[])
        client = create_client(conn)

        async with client as c:
            response = await c.get(
                "/api/v1/afk/runs",
                params={
                    "provider": "github",
                    "status": "running",
                    "outcome": "open",
                    "has_change_request": "true",
                    "created_before": "2026-08-01T00:00:00Z",
                },
            )

        assert response.status_code == 200
        sql = conn.fetch.call_args[0][0]
        assert "r.provider = $1" in sql
        assert "r.status = $2" in sql
        assert "r.outcome_status = $3" in sql
        assert "r.change_request_provider IS NOT NULL" in sql
        assert "r.first_seen_at < $4" in sql
        # limit/offset take the next placeholders after the four filters.
        assert "LIMIT $5" in sql
        assert "OFFSET $6" in sql
        assert conn.fetch.call_args[0][4] == _CUT_TS

    @pytest.mark.asyncio
    async def test_repository_filter_matches_run_or_entity(self) -> None:
        conn = _mk_conn()
        conn.fetchval = AsyncMock(return_value=0)
        conn.fetch = AsyncMock(return_value=[])
        client = create_client(conn)

        async with client as c:
            response = await c.get("/api/v1/afk/runs", params={"repository": "acme/proj"})

        assert response.status_code == 200
        sql = conn.fetch.call_args[0][0]
        assert "r.repository = $1" in sql
        assert "re.repository = $1" in sql

    @pytest.mark.asyncio
    async def test_has_change_request_false_is_unbound(self) -> None:
        conn = _mk_conn()
        conn.fetchval = AsyncMock(return_value=0)
        conn.fetch = AsyncMock(return_value=[])
        client = create_client(conn)

        async with client as c:
            response = await c.get(
                "/api/v1/afk/runs", params={"has_change_request": "false"}
            )

        assert response.status_code == 200
        sql = conn.fetch.call_args[0][0]
        assert "r.change_request_provider IS NULL" in sql

    @pytest.mark.asyncio
    async def test_bad_filters_never_touch_the_database(self) -> None:
        conn = _mk_conn()
        client = create_client(conn)

        async with client as c:
            response = await c.get("/api/v1/afk/runs", params={"has_change_request": "maybe"})

        assert response.status_code == 400
        conn.fetch.assert_not_called()
        conn.fetchval.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "params",
        [
            {"provider": "bogus"},
            {"status": "bogus"},
            {"outcome": "bogus"},
            {"created_before": "yesterday"},
            {"has_change_request": "yes"},
        ],
    )
    async def test_invalid_filters_return_400(self, params: dict) -> None:
        conn = _mk_conn()
        client = create_client(conn)

        async with client as c:
            response = await c.get("/api/v1/afk/runs", params=params)

        assert response.status_code == 400
        assert response.json()["status"] == "error"
        assert response.json()["error"]["code"] == "BAD_REQUEST"


# ══════════════════════════════════════════════════════════════════════════
#  Authorization
# ══════════════════════════════════════════════════════════════════════════


class TestAuthorization:
    """Each endpoint enforces the correct credential layer."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/api/v1/afk/runs"),
            ("GET", f"/api/v1/afk/runs/{_RUN_ID}"),
            ("PATCH", f"/api/v1/afk/runs/{_RUN_ID}"),
            ("DELETE", f"/api/v1/afk/runs/{_RUN_ID}"),
        ],
    )
    async def test_all_canonical_routes_require_api_key(self, method: str, path: str) -> None:
        conn = _mk_conn()
        client = create_client(conn, api_key=None)

        async with client as c:
            response = await c.request(method, path, json={"status": "running"})

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHORIZED"

    @pytest.mark.asyncio
    async def test_patch_requires_collector_credential(self) -> None:
        """A failed collector-credential lookup yields 401 before any row read."""
        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=None)
        client = create_client(conn)

        async with client as c:
            response = await c.patch(
                f"/api/v1/afk/runs/{_RUN_ID}", json={"status": "running"}
            )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHORIZED"

    @pytest.mark.asyncio
    async def test_patch_rejects_non_awx_credential(self) -> None:
        """A valid credential owned by another client is rejected with 403."""
        conn = _mk_conn()
        auth_row = mock_row(
            {
                "credential_id": uuid.uuid4(),
                "revoked_at": None,
                "last_used_at": None,
                "client_id": uuid.uuid4(),
                "client_name": "opencode-collector",
                "client_is_active": True,
            }
        )
        conn.fetchrow = AsyncMock(return_value=auth_row)
        client = create_client(conn)

        async with client as c:
            response = await c.patch(
                f"/api/v1/afk/runs/{_RUN_ID}", json={"status": "running"}
            )

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_delete_requires_operator_token(self) -> None:
        conn = _mk_conn()
        client = create_client(conn, operator_token=None)

        async with client as c:
            response = await c.delete(f"/api/v1/afk/runs/{_RUN_ID}")

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHORIZED"
        conn.fetchrow.assert_not_called()
        conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_delete_fails_closed_when_operator_token_unconfigured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unprovisioned ``GATEWAY_OPERATOR_TOKEN`` → 403 (no broad access)."""
        monkeypatch.delenv("GATEWAY_OPERATOR_TOKEN", raising=False)
        conn = _mk_conn()
        client = create_client(conn)

        async with client as c:
            response = await c.delete(f"/api/v1/afk/runs/{_RUN_ID}")

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"
        conn.fetchrow.assert_not_called()
        conn.execute.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════
#  Error semantics: 404 / 409 / 422 / 400
# ══════════════════════════════════════════════════════════════════════════


class TestErrorSemantics:
    """The documented error catalogue is honoured on every path."""

    @pytest.mark.asyncio
    async def test_detail_unknown_run_returns_404(self) -> None:
        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=None)
        client = create_client(conn)

        async with client as c:
            response = await c.get(f"/api/v1/afk/runs/{_RUN_ID}")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_update_unknown_run_returns_404(self) -> None:
        conn = _mk_conn()
        conn.fetchrow = AsyncMock(side_effect=[_credential_auth_row(), None])
        client = create_client(conn)

        async with client as c:
            response = await c.patch(
                f"/api/v1/afk/runs/{_RUN_ID}", json={"status": "running"}
            )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_delete_unknown_run_returns_404(self) -> None:
        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=None)
        client = create_client(conn)

        async with client as c:
            response = await c.delete(f"/api/v1/afk/runs/{_RUN_ID}")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "terminal_status", ["completed", "failed", "cancelled", "timed_out"]
    )
    async def test_update_terminal_run_is_409(self, terminal_status: str) -> None:
        conn = _mk_conn()
        stored = _mk_run_row(status=terminal_status, title="Done")
        conn.fetchrow = AsyncMock(side_effect=[_credential_auth_row(), stored])
        client = create_client(conn)

        async with client as c:
            response = await c.patch(
                f"/api/v1/afk/runs/{_RUN_ID}",
                json={"title": "Rewrite", "status": "running"},
            )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CONFLICT"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"status": "bogus"},
            {"repository": "acme/proj"},
            {"title": "   "},
            {"title": "x" * 1001},
            {"title": None},
        ],
    )
    async def test_invalid_update_bodies_return_422(self, body: dict) -> None:
        conn = _mk_conn()
        conn.fetchrow = AsyncMock(side_effect=[_credential_auth_row()])
        client = create_client(conn)

        async with client as c:
            response = await c.patch(f"/api/v1/afk/runs/{_RUN_ID}", json=body)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
        # The collector-credential gate touches ``last_used_at``; the run row
        # itself must never be mutated by an invalid body.
        executed_sql = [call[0][0] for call in conn.execute.call_args_list]
        assert not any("UPDATE afk_runs" in sql for sql in executed_sql)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/api/v1/afk/runs/not-a-ulid"),
            ("PATCH", "/api/v1/afk/runs/not-a-ulid"),
            ("GET", f"/api/v1/afk/runs/{'0' * 25}I"),
            ("DELETE", "/api/v1/afk/runs/not-a-ulid"),
        ],
    )
    async def test_malformed_ulid_returns_400(self, method: str, path: str) -> None:
        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_credential_auth_row())
        client = create_client(conn)

        async with client as c:
            response = await c.request(method, path, json={"status": "running"})

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "BAD_REQUEST"


# ══════════════════════════════════════════════════════════════════════════
#  Orphan eligibility and relationship preservation on deletion
# ══════════════════════════════════════════════════════════════════════════


class TestDeletionOrphanRules:
    """Deletion is orphan-only and preserves every linked execution record."""

    @pytest.mark.asyncio
    async def test_eligible_orphan_deletes_and_removes_only_aggregate_links(self) -> None:
        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_mk_run_row())
        conn.fetchval = AsyncMock(side_effect=[False, False])
        conn.execute = AsyncMock(
            side_effect=["DELETE 2", "DELETE 1", "DELETE 3", "DELETE 1"]
        )
        client = create_client(conn)

        async with client as c:
            response = await c.delete(f"/api/v1/afk/runs/{_RUN_ID}")

        assert response.status_code == 204
        conn.transaction.assert_called_once()

        deleted_sql = [call[0][0] for call in conn.execute.call_args_list]
        assert len(deleted_sql) == 4
        for table in (
            "afk_run_entities",
            "afk_run_sessions",
            "unresolved_correlations",
            "afk_runs",
        ):
            assert f"DELETE FROM {table} WHERE afk_run_id = $1" in deleted_sql

    @pytest.mark.asyncio
    async def test_relationship_preservation_no_execution_table_is_written(self) -> None:
        """Only the run's aggregate links are removed — execution data is not."""
        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_mk_run_row())
        conn.fetchval = AsyncMock(side_effect=[False, False])
        conn.execute = AsyncMock(
            side_effect=["DELETE 0", "DELETE 0", "DELETE 0", "DELETE 1"]
        )
        client = create_client(conn)

        async with client as c:
            response = await c.delete(f"/api/v1/afk/runs/{_RUN_ID}")

        assert response.status_code == 204
        deleted_sql = [call[0][0] for call in conn.execute.call_args_list]
        for forbidden in (
            "execution_bindings",
            "delivery_log",
            "sessions",
            "engineering_events",
        ):
            assert not any(
                f"DELETE FROM {forbidden}" in sql for sql in deleted_sql
            ), f"deletion must not touch {forbidden}"

        # Eligibility probes READ the guarded tables; they never write them.
        probe_sql = [call[0][0] for call in conn.fetchval.call_args_list]
        assert any("execution_bindings" in sql for sql in probe_sql)
        assert any("delivery_log" in sql for sql in probe_sql)

    @pytest.mark.asyncio
    async def test_execution_bindings_make_run_ineligible(self) -> None:
        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_mk_run_row())
        conn.fetchval = AsyncMock(return_value=True)
        client = create_client(conn)

        async with client as c:
            response = await c.delete(f"/api/v1/afk/runs/{_RUN_ID}")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CONFLICT"
        assert "execution bindings" in response.json()["error"]["message"]
        conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_bound_change_request_makes_run_ineligible(self) -> None:
        conn = _mk_conn()
        conn.fetchrow = AsyncMock(
            return_value=_mk_run_row(
                change_request_provider="gitlab",
                change_request_repository="acme/proj",
                change_request_external_id="442",
            )
        )
        client = create_client(conn)

        async with client as c:
            response = await c.delete(f"/api/v1/afk/runs/{_RUN_ID}")

        assert response.status_code == 409
        assert "bound change request" in response.json()["error"]["message"]
        conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_delivery_log_rows_make_run_ineligible(self) -> None:
        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_mk_run_row())
        conn.fetchval = AsyncMock(side_effect=[False, True])
        client = create_client(conn)

        async with client as c:
            response = await c.delete(f"/api/v1/afk/runs/{_RUN_ID}")

        assert response.status_code == 409
        assert "delivery log" in response.json()["error"]["message"]
        conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_ineligible_run_emits_no_delete_statements(self) -> None:
        conn = _mk_conn()
        conn.fetchrow = AsyncMock(
            return_value=_mk_run_row(
                change_request_provider="github",
                change_request_repository="acme/proj",
                change_request_external_id="99",
            )
        )
        conn.fetchval = AsyncMock(return_value=True)
        client = create_client(conn)

        async with client as c:
            response = await c.delete(f"/api/v1/afk/runs/{_RUN_ID}")

        assert response.status_code == 409
        conn.execute.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════
#  Compatibility with the execution-scoped API (/api/v1/afk/executions)
# ══════════════════════════════════════════════════════════════════════════


class TestExecutionApiCompatibility:
    """The canonical router must not change the execution-scoped surface."""

    def test_canonical_and_execution_routes_coexist(self) -> None:
        from app.core.factory import create_app

        app = create_app(configure_logging=False)
        paths = {getattr(route, "path", None) for route in app.routes}

        # Canonical AFK Run routes (issues #672/#673/#674).
        assert "/api/v1/afk/runs" in paths
        assert "/api/v1/afk/runs/{afk_run_id}" in paths
        # Execution-scoped routes remain registered unchanged (contract §6).
        assert "/api/v1/afk/executions" in paths
        assert "/api/v1/afk/executions/{awx_job_id}" in paths
        assert "/api/v1/afk/executions/runs/{afk_run_id}" in paths
        assert "/api/v1/afk/executions/runs/by-change-request" in paths

    @pytest.mark.asyncio
    async def test_execution_binding_detail_read_still_works(self) -> None:
        from tests.test_api_afk_executions import _mk_binding_row

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_mk_binding_row(awx_job_id=42))
        client = create_client(conn)

        async with client as c:
            response = await c.get("/api/v1/afk/executions/42")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["data"]["awx_job"]["job_id"] == "42"

    @pytest.mark.asyncio
    async def test_execution_binding_detail_404_and_400_unchanged(self) -> None:
        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=None)
        client = create_client(conn)
        async with client as c:
            missing = await c.get("/api/v1/afk/executions/99999")
        assert missing.status_code == 404

        conn2 = _mk_conn()
        conn2.fetchrow = AsyncMock()
        client2 = create_client(conn2)
        async with client2 as c:
            malformed = await c.get("/api/v1/afk/executions/abc")
        assert malformed.status_code == 400

    @pytest.mark.asyncio
    async def test_execution_history_list_shape_unchanged(self) -> None:
        from tests.test_api_afk_executions import _mk_binding_row

        conn = _mk_conn()
        conn.fetch = AsyncMock(
            return_value=[
                _mk_binding_row(awx_job_id=10, outcome="failed"),
                _mk_binding_row(awx_job_id=20, outcome="completed"),
            ]
        )
        client = create_client(conn)

        async with client as c:
            response = await c.get(
                "/api/v1/afk/executions",
                params={
                    "provider": "github",
                    "repository_url": "https://github.com/acme/proj",
                    "entity_type": "change_request",
                    "entity_number": "99",
                },
            )

        assert response.status_code == 200
        history = response.json()["data"]
        assert history["resource"]["provider"] == "github"
        assert [b["outcome"] for b in history["bindings"]] == ["failed", "completed"]

    @pytest.mark.asyncio
    async def test_execution_create_still_returns_201(self) -> None:
        from tests.test_api_afk_executions import (
            _auth_row,
            _mk_binding_row,
        )

        run_ulid = "01JZABCDEFGHJKLMNPQRSTVWXY"
        conn = _mk_conn()
        conn.fetchrow = AsyncMock(
            side_effect=[
                _auth_row(),
                None,
                mock_row(
                    {
                        "afk_run_id": run_ulid,
                        "change_request_provider": "github",
                        "change_request_repository": "github.com/acme/proj",
                        "change_request_external_id": "99",
                    }
                ),
                _mk_binding_row(awx_job_id=42, afk_run_id=run_ulid),
            ]
        )
        conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "42", "job_template_id": 7},
            "external_session_id": "ses_abc123",
            "resource": {
                "provider": "github",
                "repository": "https://github.com/acme/proj",
                "resource_type": "pull_request",
                "resource_number": "99",
            },
            "outcome": "completed",
            "trigger_type": "manual",
            "afk_run_id": run_ulid,
        }

        async with client as c:
            response = await c.post("/api/v1/afk/executions", json=payload)

        assert response.status_code == 201
        assert response.json()["data"]["afk_run_id"] == run_ulid

    @pytest.mark.asyncio
    async def test_execution_create_validation_unchanged_422(self) -> None:
        from tests.test_api_afk_executions import _auth_row

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_auth_row())
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "42", "job_template_id": 7},
            "external_session_id": "ses_abc123",
            "resource": {
                "provider": "github",
                "repository": "https://github.com/acme/proj",
                "resource_type": "pull_request",
                "resource_number": "99",
            },
            "outcome": "in_progress",
            "trigger_type": "manual",
        }

        async with client as c:
            response = await c.post("/api/v1/afk/executions", json=payload)

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_execution_run_scoped_read_unchanged(self) -> None:
        from tests.test_api_afk_executions import _mk_binding_row

        run_ulid = "01JZABCDEFGHJKLMNPQRSTVWXY"
        conn = _mk_conn()
        conn.fetchrow = AsyncMock(
            return_value=mock_row(
                {
                    "afk_run_id": run_ulid,
                    "provider": "github",
                    "status": "pending",
                    "host": "gateway.example",
                    "source_event_id": "evt_001",
                    "repository": "github.com/acme/proj",
                    "trigger_type": "eda",
                    "title": "Example",
                    "change_request_provider": None,
                    "change_request_repository": None,
                    "change_request_external_id": None,
                    "recovered_from_afk_run_id": None,
                    "first_seen_at": _A_TS,
                    "last_seen_at": _A_TS,
                }
            )
        )
        conn.fetch = AsyncMock(
            return_value=[_mk_binding_row(awx_job_id=10, afk_run_id=run_ulid)]
        )
        client = create_client(conn)

        async with client as c:
            response = await c.get(f"/api/v1/afk/executions/runs/{run_ulid}")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["data"][0]["awx_job"]["job_id"] == "10"

    @pytest.mark.asyncio
    async def test_execution_run_scoped_read_unknown_run_404(self) -> None:
        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=None)
        client = create_client(conn)

        async with client as c:
            response = await c.get(
                "/api/v1/afk/executions/runs/01JZABCDEFGHJKLMNPQRSTVWXY"
            )

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_execution_legacy_row_readback_unchanged(self) -> None:
        """Legacy rows without ``afk_run_id`` still read back with null fields."""
        from tests.test_api_afk_executions import _mk_binding_row

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_mk_binding_row(awx_job_id=42))
        client = create_client(conn)

        async with client as c:
            response = await c.get("/api/v1/afk/executions/42")

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["afk_run_id"] is None
        assert data["trigger_type"] is None
