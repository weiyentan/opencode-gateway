"""Tests for explicit execution→session attribution (issue #627).

Covers the ``external_session_ids`` collection contract on the
execution-binding write paths and its normalized readback:

- POST accepts the plural ``external_session_ids`` collection.
- The legacy singular ``external_session_id`` remains accepted and
  normalizes to a one-element collection.
- Persistence creates ``afk_run_sessions`` links through the existing
  AFK run-session model (one link per unique session id).
- Duplicate ids in the collection are deduplicated preserving order.
- An empty collection is rejected (422), not treated as "no sessions".
- GET/read responses expose the normalized ``external_session_ids``.

Mock-based (mirrors ``test_api_afk_executions.py``) so the suite runs
without a live database.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import mock_row

# ── Mock helpers (mirrored from test_api_afk_executions) ────────────────────

_CLIENT_ID = uuid.uuid4()
_CREDENTIAL_ID = uuid.uuid4()


def _auth_row() -> MagicMock:
    """Return a mock row that passes require_collector_token."""
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


def _mk_binding_row(
    *,
    binding_id: str = "00000000-0000-0000-0000-000000000001",
    awx_job_id: int = 42,
    job_template_id: int = 7,
    external_session_id: str | None = "ses_primary",
    provider: str | None = "github",
    repository_url: str | None = "github.com/acme/proj",
    entity_type: str | None = "change_request",
    entity_number: str | None = "99",
    outcome: str = "completed",
    afk_run_id: str | None = "01JZ0123456789ABCDEFGHJKMN",
    trigger_type: str | None = "manual",
):
    """Build a mock asyncpg Record for an execution_bindings row."""
    return mock_row(
        {
            "id": uuid.UUID(binding_id),
            "binding_id": binding_id,
            "awx_job_id": awx_job_id,
            "job_template_id": job_template_id,
            "external_session_id": external_session_id,
            "provider": provider,
            "repository_url": repository_url,
            "entity_type": entity_type,
            "entity_number": entity_number,
            "outcome": outcome,
            "source_event_id": None,
            "afk_run_id": afk_run_id,
            "trigger_type": trigger_type,
            "branch": None,
            "title": None,
            "failure_reason": None,
            "failure_summary": None,
            "started_at": None,
            "finished_at": None,
        }
    )


def _mk_conn() -> AsyncMock:
    """Mock asyncpg connection with transaction support."""
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


def _session_link_execute_sqls(conn: AsyncMock) -> list[str]:
    """Extract the SQL of every afk_run_sessions upsert issued on ``conn``."""
    sqls = []
    for call in conn.execute.call_args_list:
        sql = call.args[0] if call.args else ""
        if "INSERT INTO afk_run_sessions" in sql:
            sqls.append(sql)
    return sqls


def _session_link_params(conn: AsyncMock) -> list[tuple]:
    """Extract the parameter tuples of every afk_run_sessions upsert.

    Params follow the upsert signature:
    ``(afk_run_id, session_id, external_session_id, started_at, finished_at)``.
    """
    params = []
    for call in conn.execute.call_args_list:
        sql = call.args[0] if call.args else ""
        if "INSERT INTO afk_run_sessions" in sql:
            params.append(tuple(call.args[1:]))
    return params


# ═══════════════════════════════════════════════════════════════════════════
#  Schema contract — array input, singular compatibility, invalid input
# ═══════════════════════════════════════════════════════════════════════════


class TestSchemaContract:
    """Pydantic schema contract for the session attribution fields."""

    def test_plural_collection_accepted(self) -> None:
        from app.core.schemas.execution_binding import ExecutionBindingCreateRequest

        req = ExecutionBindingCreateRequest(
            awx_job={"job_id": "42", "job_template_id": 7},
            external_session_ids=["ses_a", "ses_b"],
            resource=_resource(),
            outcome="completed",
            trigger_type="manual",
            afk_run_id="01JZ0123456789ABCDEFGHJKMN",
        )
        assert req.normalized_session_ids() == ["ses_a", "ses_b"]

    def test_singular_normalizes_to_one_element_list(self) -> None:
        from app.core.schemas.execution_binding import ExecutionBindingCreateRequest

        req = ExecutionBindingCreateRequest(
            awx_job={"job_id": "42", "job_template_id": 7},
            external_session_id="ses_legacy",
            resource=_resource(),
            outcome="completed",
            trigger_type="manual",
            afk_run_id="01JZ0123456789ABCDEFGHJKMN",
        )
        assert req.normalized_session_ids() == ["ses_legacy"]

    def test_singular_and_plural_are_mutually_exclusive(self) -> None:
        from pydantic import ValidationError

        from app.core.schemas.execution_binding import ExecutionBindingCreateRequest

        with pytest.raises(ValidationError, match="mutually exclusive"):
            ExecutionBindingCreateRequest(
                awx_job={"job_id": "42", "job_template_id": 7},
                external_session_id="ses_legacy",
                external_session_ids=["ses_a"],
                resource=_resource(),
                outcome="completed",
                trigger_type="manual",
                afk_run_id="01JZ0123456789ABCDEFGHJKMN",
            )

    def test_empty_collection_rejected(self) -> None:
        from pydantic import ValidationError

        from app.core.schemas.execution_binding import ExecutionBindingCreateRequest

        with pytest.raises(ValidationError, match="non-empty"):
            ExecutionBindingCreateRequest(
                awx_job={"job_id": "42", "job_template_id": 7},
                external_session_ids=[],
                resource=_resource(),
                outcome="completed",
                trigger_type="manual",
                afk_run_id="01JZ0123456789ABCDEFGHJKMN",
            )

    def test_empty_string_entry_rejected(self) -> None:
        from pydantic import ValidationError

        from app.core.schemas.execution_binding import ExecutionBindingCreateRequest

        with pytest.raises(ValidationError, match="non-empty"):
            ExecutionBindingCreateRequest(
                awx_job={"job_id": "42", "job_template_id": 7},
                external_session_ids=["ses_a", ""],
                resource=_resource(),
                outcome="completed",
                trigger_type="manual",
                afk_run_id="01JZ0123456789ABCDEFGHJKMN",
            )

    def test_duplicates_deduplicated_preserving_order(self) -> None:
        from app.core.schemas.execution_binding import ExecutionBindingCreateRequest

        req = ExecutionBindingCreateRequest(
            awx_job={"job_id": "42", "job_template_id": 7},
            external_session_ids=["ses_a", "ses_b", "ses_a", "ses_c", "ses_b"],
            resource=_resource(),
            outcome="completed",
            trigger_type="manual",
            afk_run_id="01JZ0123456789ABCDEFGHJKMN",
        )
        assert req.normalized_session_ids() == ["ses_a", "ses_b", "ses_c"]

    def test_completed_requires_session_in_either_form(self) -> None:
        """A completed execution with no session in either form is rejected."""
        from pydantic import ValidationError

        from app.core.schemas.execution_binding import ExecutionBindingCreateRequest

        with pytest.raises(ValidationError, match="outcome is 'completed'"):
            ExecutionBindingCreateRequest(
                awx_job={"job_id": "42", "job_template_id": 7},
                resource=_resource(),
                outcome="completed",
                trigger_type="manual",
                afk_run_id="01JZ0123456789ABCDEFGHJKMN",
            )

    def test_update_request_plural_accepted_and_singular_normalizes(self) -> None:
        from app.core.schemas.execution_binding import ExecutionBindingUpdateRequest

        plural = ExecutionBindingUpdateRequest(
            outcome="completed",
            external_session_ids=["ses_a", "ses_b"],
        )
        assert plural.normalized_session_ids() == ["ses_a", "ses_b"]

        singular = ExecutionBindingUpdateRequest(
            outcome="completed",
            external_session_id="ses_legacy",
        )
        assert singular.normalized_session_ids() == ["ses_legacy"]

    def test_update_request_mutual_exclusion_and_empty(self) -> None:
        from pydantic import ValidationError

        from app.core.schemas.execution_binding import ExecutionBindingUpdateRequest

        with pytest.raises(ValidationError, match="mutually exclusive"):
            ExecutionBindingUpdateRequest(
                outcome="completed",
                external_session_id="ses_legacy",
                external_session_ids=["ses_a"],
            )
        with pytest.raises(ValidationError, match="non-empty"):
            ExecutionBindingUpdateRequest(outcome="completed", external_session_ids=[])


# ═══════════════════════════════════════════════════════════════════════════
#  POST persistence — durable execution→session associations
# ═══════════════════════════════════════════════════════════════════════════


class TestPostSessionAttributionPersistence:
    """POST persists afk_run_sessions links for the normalized sessions."""

    @pytest.mark.asyncio
    async def test_plural_input_persists_one_link_per_session(self) -> None:
        """Each unique session id in the collection gets its own link."""
        from tests.conftest import create_client

        conn = _mk_conn()
        saved_row = _mk_binding_row(awx_job_id=42)
        conn.fetchrow = AsyncMock(
            side_effect=[
                _auth_row(),
                None,  # no existing binding
                mock_row(  # run exists (change request matches the resource)
                    {
                        "afk_run_id": "01JZ0123456789ABCDEFGHJKMN",
                        "change_request_provider": "github",
                        "change_request_repository": "github.com/acme/proj",
                        "change_request_external_id": "99",
                    }
                ),
                saved_row,
            ]
        )
        conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "42", "job_template_id": 7},
            "external_session_ids": ["ses_a", "ses_b"],
            "resource": _resource(),
            "outcome": "completed",
            "trigger_type": "manual",
            "afk_run_id": "01JZ0123456789ABCDEFGHJKMN",
        }
        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201, resp.text

        params = _session_link_params(conn)
        assert len(params) == 2
        run_ids = {p[0] for p in params}
        session_ids = [p[2] for p in params]
        assert session_ids == ["ses_a", "ses_b"]
        # All links attach to the same (auto-provisioned) AFK run.
        assert len(run_ids) == 1

    @pytest.mark.asyncio
    async def test_singular_input_persists_exactly_one_link(self) -> None:
        """The legacy singular form still creates exactly one session link."""
        from tests.conftest import create_client

        conn = _mk_conn()
        saved_row = _mk_binding_row(awx_job_id=43)
        conn.fetchrow = AsyncMock(
            side_effect=[
                _auth_row(),
                None,  # no existing binding
                mock_row(  # run exists (change request matches the resource)
                    {
                        "afk_run_id": "01JZ0123456789ABCDEFGHJKMN",
                        "change_request_provider": "github",
                        "change_request_repository": "github.com/acme/proj",
                        "change_request_external_id": "99",
                    }
                ),
                saved_row,
            ]
        )
        conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "43", "job_template_id": 7},
            "external_session_id": "ses_legacy",
            "resource": _resource(),
            "outcome": "completed",
            "trigger_type": "manual",
            "afk_run_id": "01JZ0123456789ABCDEFGHJKMN",
        }
        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201, resp.text

        params = _session_link_params(conn)
        assert len(params) == 1
        assert params[0][2] == "ses_legacy"

    @pytest.mark.asyncio
    async def test_duplicate_ids_persist_single_link_per_unique_session(self) -> None:
        """Duplicate ids collapse to one link per unique session id."""
        from tests.conftest import create_client

        conn = _mk_conn()
        saved_row = _mk_binding_row(awx_job_id=44)
        conn.fetchrow = AsyncMock(
            side_effect=[
                _auth_row(),
                None,  # no existing binding
                mock_row(  # run exists (change request matches the resource)
                    {
                        "afk_run_id": "01JZ0123456789ABCDEFGHJKMN",
                        "change_request_provider": "github",
                        "change_request_repository": "github.com/acme/proj",
                        "change_request_external_id": "99",
                    }
                ),
                saved_row,
            ]
        )
        conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "44", "job_template_id": 7},
            "external_session_ids": ["ses_a", "ses_b", "ses_a"],
            "resource": _resource(),
            "outcome": "completed",
            "trigger_type": "manual",
            "afk_run_id": "01JZ0123456789ABCDEFGHJKMN",
        }
        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201, resp.text

        params = _session_link_params(conn)
        assert [p[2] for p in params] == ["ses_a", "ses_b"]

    @pytest.mark.asyncio
    async def test_no_session_persists_no_link(self) -> None:
        """A binding without session attribution creates no session links."""
        from tests.conftest import create_client

        conn = _mk_conn()
        saved_row = _mk_binding_row(
            awx_job_id=45,
            external_session_id=None,
            provider=None,
            repository_url=None,
            entity_type=None,
            entity_number=None,
            outcome="failed",
        )
        conn.fetchrow = AsyncMock(
            side_effect=[
                _auth_row(),
                None,  # no existing binding
                mock_row({"afk_run_id": "01JZ0123456789ABCDEFGHJKMN"}),  # run exists
                saved_row,
            ]
        )
        conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "45", "job_template_id": 7},
            "afk_run_id": "01JZ0123456789ABCDEFGHJKMN",
            "outcome": "failed",
            "trigger_type": "manual",
            "failure_reason": "boom",
        }
        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201, resp.text
        assert _session_link_execute_sqls(conn) == []

    @pytest.mark.asyncio
    async def test_empty_collection_rejected_422(self) -> None:
        """An explicitly empty collection is invalid input (422)."""
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(side_effect=[_auth_row()])
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "46", "job_template_id": 7},
            "external_session_ids": [],
            "resource": _resource(),
            "outcome": "completed",
            "trigger_type": "manual",
            "afk_run_id": "01JZ0123456789ABCDEFGHJKMN",
        }
        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 422
        body = resp.json()
        assert body["status"] == "error"

    @pytest.mark.asyncio
    async def test_singular_and_plural_together_rejected_422(self) -> None:
        """Supplying both the singular and the plural form is a 422."""
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(side_effect=[_auth_row()])
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "47", "job_template_id": 7},
            "external_session_id": "ses_legacy",
            "external_session_ids": ["ses_a"],
            "resource": _resource(),
            "outcome": "completed",
            "trigger_type": "manual",
            "afk_run_id": "01JZ0123456789ABCDEFGHJKMN",
        }
        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_links_resolve_internal_session_id_best_effort(self) -> None:
        """The link carries a resolved internal session UUID when exactly
        one Gateway session matches, else NULL (issue #618 semantics)."""
        from tests.conftest import create_client

        conn = _mk_conn()
        saved_row = _mk_binding_row(awx_job_id=48)
        internal_uuid = uuid.uuid4()
        conn.fetchrow = AsyncMock(
            side_effect=[
                _auth_row(),
                None,  # no existing binding
                mock_row(  # run exists (change request matches the resource)
                    {
                        "afk_run_id": "01JZ0123456789ABCDEFGHJKMN",
                        "change_request_provider": "github",
                        "change_request_repository": "github.com/acme/proj",
                        "change_request_external_id": "99",
                    }
                ),
                saved_row,
            ]
        )
        conn.fetch = AsyncMock(
            side_effect=[
                [],  # project completed-run status (no bindings yet)
                [mock_row({"id": uuid.uuid4()})],  # binding RETURNING id
                [mock_row({"id": internal_uuid})],  # resolve ses_a → 1 match
                [],  # resolve ses_b → 0 matches
                [mock_row({"outcome": "completed"})],  # converge projection
            ]
        )
        conn.execute = AsyncMock()
        client = create_client(conn)

        payload = {
            "awx_job": {"job_id": "48", "job_template_id": 7},
            "external_session_ids": ["ses_a", "ses_b"],
            "resource": _resource(),
            "outcome": "completed",
            "trigger_type": "manual",
            "afk_run_id": "01JZ0123456789ABCDEFGHJKMN",
        }
        resp = await client.post("/api/v1/afk/executions", json=payload)
        assert resp.status_code == 201, resp.text

        params = _session_link_params(conn)
        assert len(params) == 2
        assert params[0][1] == str(internal_uuid)  # ses_a resolved
        assert params[1][1] is None  # ses_b unresolved → NULL


# ═══════════════════════════════════════════════════════════════════════════
#  PATCH persistence — session fill-ins create associations
# ═══════════════════════════════════════════════════════════════════════════


class TestPatchSessionAttributionPersistence:
    """PATCH terminal updates persist associations for fill-in sessions."""

    @pytest.mark.asyncio
    async def test_patch_plural_persists_links_for_each_session(self) -> None:
        from unittest.mock import patch

        from afk_outcomes.models import ExecutionBinding, ExecutionOutcome
        from afk_outcomes.repository import UpdateExecutionBindingResult
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(side_effect=[_auth_row()])
        conn.execute = AsyncMock()
        client = create_client(conn)

        domain_binding = ExecutionBinding(
            binding_id="00000000-0000-0000-0000-000000000050",
            awx_job={"job_id": "50", "job_template_id": 7},
            external_session_ids=["ses_p1", "ses_p2"],
            resource={
                "provider": __import__(
                    "afk_outcomes.models", fromlist=["Provider"]
                ).Provider.GITHUB,
                "repository": "github.com/acme/proj",
                "resource_type": __import__(
                    "afk_outcomes.models", fromlist=["EntityType"]
                ).EntityType.CHANGE_REQUEST,
                "resource_number": "99",
            },
            outcome=ExecutionOutcome.COMPLETED,
            afk_run_id="01JZ0123456789ABCDEFGHJKMN",
            trigger_type="manual",
        )
        with patch(
            "app.api.afk_executions.AsyncpgOutcomeRepository.update_execution_binding_terminal",
            new=AsyncMock(return_value=UpdateExecutionBindingResult(is_updated=True)),
        ), patch(
            "app.api.afk_executions.AsyncpgOutcomeRepository.get_execution_binding_by_awx_job_id",
            new=AsyncMock(return_value=domain_binding),
        ):
            resp = await client.patch(
                "/api/v1/afk/executions/50",
                json={
                    "outcome": "completed",
                    "external_session_ids": ["ses_p1", "ses_p2"],
                    "resource": _resource(),
                },
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["external_session_ids"] == ["ses_p1", "ses_p2"]
        assert data["external_session_id"] == "ses_p1"

    @pytest.mark.asyncio
    async def test_patch_invalid_empty_collection_rejected_422(self) -> None:
        from tests.conftest import create_client

        conn = _mk_conn()
        conn.fetchrow = AsyncMock(side_effect=[_auth_row()])
        client = create_client(conn)

        resp = await client.patch(
            "/api/v1/afk/executions/51",
            json={"outcome": "completed", "external_session_ids": []},
        )
        assert resp.status_code == 422


# ═══════════════════════════════════════════════════════════════════════════
#  Read model — normalized session attribution in responses
# ═══════════════════════════════════════════════════════════════════════════


class TestReadSessionAttribution:
    """GET responses expose the normalized external_session_ids."""

    @pytest.mark.asyncio
    async def test_get_binding_with_session_returns_collection(self) -> None:
        """A binding with a singular stored session reads back a 1-element
        collection plus the legacy singular mirror."""
        from tests.conftest import create_client

        conn = _mk_conn()
        row = _mk_binding_row(awx_job_id=42)
        conn.fetchrow = AsyncMock(return_value=row)
        client = create_client(conn)

        resp = await client.get("/api/v1/afk/executions/42")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["external_session_ids"] == ["ses_primary"]
        assert data["external_session_id"] == "ses_primary"

    @pytest.mark.asyncio
    async def test_get_binding_without_session_returns_empty_collection(self) -> None:
        """A historical / run-level-only binding reads back an empty
        collection (never fabricated ownership)."""
        from tests.conftest import create_client

        conn = _mk_conn()
        row = _mk_binding_row(awx_job_id=42, external_session_id=None)
        conn.fetchrow = AsyncMock(return_value=row)
        client = create_client(conn)

        resp = await client.get("/api/v1/afk/executions/42")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["external_session_ids"] == []
        assert data["external_session_id"] is None

    @pytest.mark.asyncio
    async def test_list_history_includes_session_collection(self) -> None:
        """The resource-history read includes external_session_ids per binding."""
        from tests.conftest import create_client

        conn = _mk_conn()
        rows = [
            _mk_binding_row(
                binding_id="00000000-0000-0000-0000-00000000000a",
                awx_job_id=60,
                external_session_id="ses_h1",
            ),
            _mk_binding_row(
                binding_id="00000000-0000-0000-0000-00000000000b",
                awx_job_id=61,
                external_session_id=None,
            ),
        ]
        conn.fetch = AsyncMock(return_value=rows)
        client = create_client(conn)

        resp = await client.get(
            "/api/v1/afk/executions",
            params={
                "provider": "github",
                "repository_url": "https://github.com/acme/proj",
                "entity_type": "change_request",
                "entity_number": "99",
            },
        )
        assert resp.status_code == 200
        bindings = resp.json()["data"]["bindings"]
        assert bindings[0]["external_session_ids"] == ["ses_h1"]
        assert bindings[1]["external_session_ids"] == []


# ═══════════════════════════════════════════════════════════════════════════
#  Repository read — ExecutionBinding domain model normalization
# ═══════════════════════════════════════════════════════════════════════════


class TestRowToExecutionBinding:
    """_row_to_execution_binding derives external_session_ids from storage."""

    def test_singular_column_normalizes_to_one_element(self) -> None:
        from afk_outcomes.repository import _row_to_execution_binding

        row = _mk_binding_row(external_session_id="ses_one")
        binding = _row_to_execution_binding(row)
        assert binding.external_session_ids == ["ses_one"]
        assert binding.external_session_id == "ses_one"

    def test_null_column_normalizes_to_empty(self) -> None:
        from afk_outcomes.repository import _row_to_execution_binding

        row = _mk_binding_row(external_session_id=None)
        binding = _row_to_execution_binding(row)
        assert binding.external_session_ids == []
        assert binding.external_session_id is None

    def test_jsonb_multi_session_column_reads_back_normalized(self) -> None:
        """When the JSONB attribution column exists (migration 0042), the
        full deduplicated collection reads back and the singular column
        mirrors the first entry."""
        from afk_outcomes.repository import _row_to_execution_binding

        row = _mk_binding_row(external_session_id="ses_first")
        # Simulate the additive JSONB column on the row.
        data = {
            "id": row["id"],
            "awx_job_id": row["awx_job_id"],
            "job_template_id": row["job_template_id"],
            "external_session_id": "ses_first",
            "external_session_ids_json": json.dumps(["ses_first", "ses_second"]),
            "provider": row["provider"],
            "repository_url": row["repository_url"],
            "entity_type": row["entity_type"],
            "entity_number": row["entity_number"],
            "outcome": row["outcome"],
            "source_event_id": None,
            "branch": None,
            "title": None,
            "failure_reason": None,
            "failure_summary": None,
            "started_at": None,
            "finished_at": None,
            "afk_run_id": row["afk_run_id"],
            "trigger_type": row["trigger_type"],
        }
        enriched = mock_row(data)
        binding = _row_to_execution_binding(enriched)
        assert binding.external_session_ids == ["ses_first", "ses_second"]
        assert binding.external_session_id == "ses_first"
