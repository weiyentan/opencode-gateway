"""Tests for batch provenance and execution-binding multiplicity (issue #595).

Covers the two contracts added on top of the provisional lifecycle (#589):

1. **Batch provenance** — provisioning preserves the first triggering
   delivery on the run (``afk_runs.first_delivery_id``) and every
   contributing delivery identity as a batch record
   (``afk_run_delivery_batches``).  Replays with an identical batch are
   idempotent; a different or omitted batch is a conflict, never an
   erasure; a failed batch write rolls the run insert back.

2. **Execution-binding multiplicity** — an execution callback may supply a
   pre-provisioned ``afk_run_id`` so many bindings (a retry with a new
   ``awx_job_id``) reference one lifecycle.  An unknown ``afk_run_id`` is
   rejected (``run_missing``); a legacy callback without ``afk_run_id``
   preserves the auto-provision behavior; legacy replays never conflict on
   the stored auto-created run.

Plus the 0040 migration contract (offline render + ORM mirror) and the API
surface (response provenance, 201/200/404/409 mapping, auth, validation).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import io
import re
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from afk_outcomes import AsyncpgOutcomeRepository
from afk_outcomes.models import ExecutionOutcome, Provider, TriggerType
from afk_outcomes.repository import (
    CreateAFKExecutionBindingResult,
    ProvisionAFKRunResult,
)
from afk_outcomes.serialization import SequenceULID
from tests.conftest import create_client, mock_row

_PROJ_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_DIR = _PROJ_ROOT / "alembic"
_MIGRATION_FILE = _ALEMBIC_DIR / "versions" / "0040_afk_run_batch_provenance.py"

_ULID_SOURCE = SequenceULID(1_700_000_000_000, start=1)
_NEW_ULID = "01HF7YAT000000000000000001"
_SUPPLIED_RUN_ID = "01HSUPPLIED000000000000001"
_RUN_ID = "01HRUN00000000000000000001"

_CLIENT_ID = uuid.uuid4()
_CREDENTIAL_ID = uuid.uuid4()

_BATCH_COLUMNS = ("first_delivery_id",)


def _run(coro):
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════


def _provision_kwargs(**overrides) -> dict:
    payload = {
        "provider": Provider.GITHUB,
        "host": "awx-01.internal",
        "source_event_id": "eda-1234",
        "repository": "github.com/acme/proj",
        "trigger_type": TriggerType.EDA,
        "title": "Implement auth",
        "recovered_from_afk_run_id": None,
        "deliveries": None,
        "ulid_source": _ULID_SOURCE,
    }
    payload.update(overrides)
    return payload


def _existing_key_row(**overrides) -> MagicMock:
    row = {
        "afk_run_id": _NEW_ULID,
        "repository": "github.com/acme/proj",
        "trigger_type": "eda",
        "title": "Implement auth",
        "recovered_from_afk_run_id": None,
        "first_delivery_id": None,
    }
    row.update(overrides)
    return mock_row(row)


def _binding_kwargs(**overrides) -> dict:
    payload = {
        "awx_job_id": "700",
        "job_template_id": 42,
        "provider": Provider.GITHUB,
        "repository": "org/repo",
        "resource_number": "42",
        "external_session_id": "ses_xyz",
        "outcome": ExecutionOutcome.COMPLETED,
        "source_event_id": None,
        "branch": "main",
        "title": "Test task",
        "failure_reason": None,
        "started_at": None,
        "finished_at": None,
        "trigger_type": None,
        "afk_run_id": None,
        "ulid_source": _ULID_SOURCE,
    }
    payload.update(overrides)
    return payload


def _existing_binding_row(**overrides) -> MagicMock:
    row = {
        "id": uuid.uuid4(),
        "afk_run_id": _SUPPLIED_RUN_ID,
        "awx_job_id": 700,
        "outcome": "completed",
        "title": "Test task",
        "branch": "main",
        "failure_reason": None,
        "source_event_id": None,
        "external_session_id": "ses_xyz",
        "trigger_type": None,
    }
    row.update(overrides)
    return mock_row(row)


def _calls_matching(conn: AsyncMock, pattern: str) -> list[tuple]:
    results = []
    for call in conn.fetch.call_args_list + conn.fetchrow.call_args_list:
        if call.args and re.search(pattern, call.args[0]):
            results.append((call.args[0], call.args[1:]))
    for call in conn.execute.call_args_list:
        if call.args and re.search(pattern, call.args[0]):
            results.append((call.args[0], call.args[1:]))
    return results


def _auth_row() -> MagicMock:
    """Return a mock row that passes require_collector_token (dedicated client)."""
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
    """Build a mock asyncpg connection with transaction support."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    mock_tx = AsyncMock()
    mock_tx.__aenter__ = AsyncMock(return_value=mock_tx)
    mock_tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=mock_tx)
    return conn


# ══════════════════════════════════════════════════════════════════════════
#  Repository — provisioning batch provenance
# ══════════════════════════════════════════════════════════════════════════


class TestProvisionBatchProvenance:
    """provision_afk_run with ``deliveries`` (issue #595)."""

    def test_first_creation_writes_first_delivery_and_batch_rows(
        self, mock_conn: AsyncMock
    ) -> None:
        """The run row carries first_delivery_id and every identity becomes a batch row."""
        mock_conn.fetchrow = AsyncMock(return_value=None)
        mock_conn.fetch = AsyncMock(return_value=[mock_row({"afk_run_id": _NEW_ULID})])
        mock_conn.execute = AsyncMock()

        repo = AsyncpgOutcomeRepository(mock_conn)
        result = _run(
            repo.provision_afk_run(**_provision_kwargs(deliveries=["d1", "d2"]))
        )

        assert isinstance(result, ProvisionAFKRunResult)
        assert result.is_created is True
        assert result.is_conflict is False

        # The run INSERT carries first_delivery_id = deliveries[0] (appended
        # after recovered_from_afk_run_id so earlier positions are stable).
        run_inserts = _calls_matching(mock_conn, r"INSERT INTO afk_runs")
        assert len(run_inserts) == 1
        sql, args = run_inserts[0]
        assert "first_delivery_id" in sql
        assert args[8] == "d1"

        # One batch INSERT carrying the ordered delivery identities as an
        # array (unnest ... WITH ORDINALITY), conflict-idempotent.
        batch_inserts = _calls_matching(
            mock_conn, r"INSERT INTO afk_run_delivery_batches"
        )
        assert len(batch_inserts) == 1
        assert batch_inserts[0][1][1:] == (["d1", "d2"],)
        assert "ON CONFLICT (afk_run_id, delivery_id) DO NOTHING" in batch_inserts[0][0]

    def test_replay_with_same_batch_is_idempotent(self, mock_conn: AsyncMock) -> None:
        """An identical batch replay issues no writes."""
        mock_conn.fetchrow = AsyncMock(
            return_value=_existing_key_row(first_delivery_id="d1")
        )
        mock_conn.fetch = AsyncMock(
            return_value=[mock_row({"delivery_id": "d1"}), mock_row({"delivery_id": "d2"})]
        )

        repo = AsyncpgOutcomeRepository(mock_conn)
        result = _run(
            repo.provision_afk_run(**_provision_kwargs(deliveries=["d1", "d2"]))
        )

        assert result.afk_run_id == _NEW_ULID
        assert result.is_created is False
        assert result.is_conflict is False
        assert _calls_matching(mock_conn, r"INSERT INTO afk_runs") == []
        assert _calls_matching(mock_conn, r"INSERT INTO afk_run_delivery_batches") == []

    def test_replay_with_different_batch_is_conflict(self, mock_conn: AsyncMock) -> None:
        """A replay supplying a different batch is rejected without mutation."""
        mock_conn.fetchrow = AsyncMock(
            return_value=_existing_key_row(first_delivery_id="d1")
        )
        mock_conn.fetch = AsyncMock(return_value=[mock_row({"delivery_id": "d1"})])

        repo = AsyncpgOutcomeRepository(mock_conn)
        result = _run(
            repo.provision_afk_run(**_provision_kwargs(deliveries=["d1", "d3"]))
        )

        assert result.is_conflict is True
        assert result.is_created is False
        assert _calls_matching(mock_conn, r"INSERT INTO afk_runs") == []
        assert _calls_matching(mock_conn, r"INSERT INTO afk_run_delivery_batches") == []

    def test_replay_omitting_batch_is_conflict_never_erasure(
        self, mock_conn: AsyncMock
    ) -> None:
        """A replay that omits deliveries against a run with provenance conflicts."""
        mock_conn.fetchrow = AsyncMock(
            return_value=_existing_key_row(first_delivery_id="d1")
        )

        repo = AsyncpgOutcomeRepository(mock_conn)
        # provisioning_payload_matches returns True (all non-batch fields
        # match); _batch_provenance_matches returns False because the stored
        # batch carries deliveries ("d1") while the replay omits them — a
        # conflict, never an erasure.
        with patch.object(
            repo, "_batch_provenance_matches", AsyncMock(return_value=False)
        ):
            result = _run(
                repo.provision_afk_run(**_provision_kwargs(deliveries=None))
            )

        assert result.is_conflict is True
        assert result.is_created is False

    def test_legacy_creation_without_batch_writes_no_provenance(
        self, mock_conn: AsyncMock
    ) -> None:
        """Provisioning without deliveries preserves the legacy no-batch shape."""
        mock_conn.fetchrow = AsyncMock(return_value=None)
        mock_conn.fetch = AsyncMock(return_value=[mock_row({"afk_run_id": _NEW_ULID})])
        mock_conn.execute = AsyncMock()

        repo = AsyncpgOutcomeRepository(mock_conn)
        result = _run(repo.provision_afk_run(**_provision_kwargs()))

        assert result.is_created is True
        run_inserts = _calls_matching(mock_conn, r"INSERT INTO afk_runs")
        assert run_inserts[0][1][8] is None  # first_delivery_id is NULL
        assert _calls_matching(mock_conn, r"INSERT INTO afk_run_delivery_batches") == []

    def test_duplicate_deliveries_are_deduplicated(self, mock_conn: AsyncMock) -> None:
        """Duplicate identities in one batch dedup preserving order."""
        mock_conn.fetchrow = AsyncMock(return_value=None)
        mock_conn.fetch = AsyncMock(return_value=[mock_row({"afk_run_id": _NEW_ULID})])
        mock_conn.execute = AsyncMock()

        repo = AsyncpgOutcomeRepository(mock_conn)
        result = _run(
            repo.provision_afk_run(**_provision_kwargs(deliveries=["d1", "d1", "d2"]))
        )

        assert result.is_created is True
        batch_inserts = _calls_matching(
            mock_conn, r"INSERT INTO afk_run_delivery_batches"
        )
        assert [c[1][1:] for c in batch_inserts] == [(["d1", "d2"],)]

    def test_failed_batch_write_rolls_back_the_run(self, mock_conn: AsyncMock) -> None:
        """A batch-write failure propagates — the savepoint rolls the run back."""
        mock_conn.fetchrow = AsyncMock(return_value=None)
        mock_conn.fetch = AsyncMock(return_value=[mock_row({"afk_run_id": _NEW_ULID})])
        mock_conn.execute = AsyncMock(side_effect=asyncpg.PostgresError("boom"))

        repo = AsyncpgOutcomeRepository(mock_conn)
        with pytest.raises(asyncpg.PostgresError):
            _run(repo.provision_afk_run(**_provision_kwargs(deliveries=["d1"])))

        # The batch write ran inside the provisioning transaction — no
        # success result escaped the failure.
        mock_conn.transaction.assert_called_once()


class TestReadBatchProvenance:
    """get_afk_run_batch_provenance (issue #595)."""

    def test_returns_first_and_ordered_deliveries(self, mock_conn: AsyncMock) -> None:
        mock_conn.fetch = AsyncMock(
            return_value=[
                mock_row({"first_delivery_id": "d1", "delivery_id": "d1"}),
                mock_row({"first_delivery_id": "d1", "delivery_id": "d2"}),
            ]
        )

        repo = AsyncpgOutcomeRepository(mock_conn)
        first, delivery_ids = _run(repo.get_afk_run_batch_provenance(_NEW_ULID))

        assert first == "d1"
        assert delivery_ids == ["d1", "d2"]

    def test_returns_empty_for_unknown_or_legacy_run(
        self, mock_conn: AsyncMock
    ) -> None:
        mock_conn.fetch = AsyncMock(return_value=[])

        repo = AsyncpgOutcomeRepository(mock_conn)
        first, delivery_ids = _run(repo.get_afk_run_batch_provenance(_NEW_ULID))

        assert first is None
        assert delivery_ids == []


# ══════════════════════════════════════════════════════════════════════════
#  Repository — execution-binding multiplicity
# ══════════════════════════════════════════════════════════════════════════


class TestExecutionBindingMultiplicity:
    """create_or_replay_afk_execution_binding with ``afk_run_id`` (issue #595)."""

    def test_supplied_run_attaches_without_creating_a_run(
        self, mock_conn: AsyncMock
    ) -> None:
        """A supplied afk_run_id links the binding to the existing lifecycle."""
        mock_conn.fetchrow = AsyncMock(
            side_effect=[
                None,  # no existing binding
                mock_row({"afk_run_id": _SUPPLIED_RUN_ID}),
                None,  # no other lifecycle owns this change request
            ]
        )
        mock_conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        mock_conn.execute = AsyncMock(return_value="UPDATE 1")

        repo = AsyncpgOutcomeRepository(mock_conn)
        result = _run(
            repo.create_or_replay_afk_execution_binding(
                **_binding_kwargs(afk_run_id=_SUPPLIED_RUN_ID)
            )
        )

        assert isinstance(result, CreateAFKExecutionBindingResult)
        assert result.is_created is True
        assert result.run_missing is False
        assert result.afk_run_id == _SUPPLIED_RUN_ID
        # No new afk_runs row was created.
        assert _calls_matching(mock_conn, r"INSERT INTO afk_runs") == []
        # The binding INSERT carries the supplied run id.
        binding_inserts = _calls_matching(mock_conn, r"INSERT INTO execution_bindings")
        assert _SUPPLIED_RUN_ID in binding_inserts[0][1]

    def test_unknown_supplied_run_is_run_missing(self, mock_conn: AsyncMock) -> None:
        """A supplied afk_run_id referencing no lifecycle is flagged, not created."""
        mock_conn.fetchrow = AsyncMock(side_effect=[None, None])

        repo = AsyncpgOutcomeRepository(mock_conn)
        result = _run(
            repo.create_or_replay_afk_execution_binding(
                **_binding_kwargs(afk_run_id="01HMISSING0000000000000001")
            )
        )

        assert result.run_missing is True
        assert result.is_created is False
        assert result.is_conflict is False
        assert _calls_matching(mock_conn, r"INSERT INTO execution_bindings") == []
        assert _calls_matching(mock_conn, r"INSERT INTO afk_runs") == []

    def test_legacy_callback_without_run_still_auto_provisions(
        self, mock_conn: AsyncMock
    ) -> None:
        """Omitting afk_run_id preserves the legacy auto-provision behavior."""
        mock_conn.fetchrow = AsyncMock(return_value=None)
        mock_conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        mock_conn.execute = AsyncMock()

        repo = AsyncpgOutcomeRepository(mock_conn)
        # A fresh deterministic source — the module-level source is shared
        # across tests, so its counter has already advanced.
        source = SequenceULID(1_700_000_000_000, start=1)
        result = _run(
            repo.create_or_replay_afk_execution_binding(
                **_binding_kwargs(ulid_source=source)
            )
        )

        assert result.is_created is True
        assert result.afk_run_id == _NEW_ULID
        assert len(_calls_matching(mock_conn, r"INSERT INTO afk_runs")) == 1

    def test_replay_with_matching_run_is_idempotent(self, mock_conn: AsyncMock) -> None:
        """Replaying with the same afk_run_id is a no-op."""
        mock_conn.fetchrow = AsyncMock(return_value=_existing_binding_row())

        repo = AsyncpgOutcomeRepository(mock_conn)
        result = _run(
            repo.create_or_replay_afk_execution_binding(
                **_binding_kwargs(afk_run_id=_SUPPLIED_RUN_ID)
            )
        )

        assert result.is_conflict is False
        assert result.is_created is False
        assert result.afk_run_id == _SUPPLIED_RUN_ID

    def test_replay_with_different_run_is_conflict(self, mock_conn: AsyncMock) -> None:
        """Supplying a different afk_run_id on replay is a conflict."""
        mock_conn.fetchrow = AsyncMock(return_value=_existing_binding_row())

        repo = AsyncpgOutcomeRepository(mock_conn)
        result = _run(
            repo.create_or_replay_afk_execution_binding(
                **_binding_kwargs(afk_run_id=_RUN_ID)
            )
        )

        assert result.is_conflict is True
        assert result.is_created is False

    def test_legacy_replay_omitting_run_never_conflicts(
        self, mock_conn: AsyncMock
    ) -> None:
        """A legacy replay without afk_run_id is idempotent against a stored run."""
        mock_conn.fetchrow = AsyncMock(return_value=_existing_binding_row())

        repo = AsyncpgOutcomeRepository(mock_conn)
        result = _run(
            repo.create_or_replay_afk_execution_binding(**_binding_kwargs())
        )

        assert result.is_conflict is False
        assert result.is_created is False
        assert result.afk_run_id == _SUPPLIED_RUN_ID


# ══════════════════════════════════════════════════════════════════════════
#  API — batch provenance response + execution-binding multiplicity
# ══════════════════════════════════════════════════════════════════════════


def _mk_lifecycle_row(**overrides) -> MagicMock:
    row = {
        "afk_run_id": _RUN_ID,
        "provider": "github",
        "status": "pending",
        "host": "awx-01.internal",
        "source_event_id": "eda-1234",
        "repository": "github.com/acme/proj",
        "trigger_type": "eda",
        "title": "Implement auth",
        "change_request_provider": None,
        "change_request_repository": None,
        "change_request_external_id": None,
        "recovered_from_afk_run_id": None,
        "first_seen_at": None,
        "last_seen_at": None,
    }
    row.update(overrides)
    return mock_row(row)


def _mk_binding_row(**overrides) -> MagicMock:
    row = {
        "id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
        "binding_id": "00000000-0000-0000-0000-000000000001",
        "awx_job_id": 42,
        "job_template_id": 7,
        "external_session_id": "ses_abc123",
        "provider": "github",
        "repository_url": "github.com/acme/proj",
        "entity_type": "change_request",
        "entity_number": "99",
        "outcome": "completed",
        "source_event_id": None,
        "afk_run_id": None,
        "trigger_type": "manual",
        "branch": None,
        "title": None,
        "failure_reason": None,
        "started_at": None,
        "finished_at": None,
    }
    row.update(overrides)
    return mock_row(row)


def _provision_payload(**overrides) -> dict:
    payload = {
        "provider": "github",
        "host": "awx-01.internal",
        "source_event_id": "eda-1234",
        "repository": "https://github.com/acme/proj",
        "trigger_type": "eda",
        "title": "Implement auth",
    }
    payload.update(overrides)
    return payload


def _execution_payload(**overrides) -> dict:
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
    }
    payload.update(overrides)
    return payload


class TestProvisionApiBatchProvenance:
    """POST /api/v1/afk/executions/runs — batch provenance surface."""

    @pytest.mark.asyncio
    async def test_provision_surfaces_batch_provenance(self) -> None:
        """A provisioned lifecycle with a batch returns first_delivery_id + delivery_ids."""
        conn = _mk_conn()
        # auth → existing (None) → readback → provenance (fetch)
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), None, _mk_lifecycle_row()]
        )
        conn.fetch = AsyncMock(
            side_effect=[
                [mock_row({"afk_run_id": _RUN_ID})],
                [
                    mock_row({"first_delivery_id": "d1", "delivery_id": "d1"}),
                    mock_row({"first_delivery_id": "d1", "delivery_id": "d2"}),
                ],
            ]
        )
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.post(
            "/api/v1/afk/executions/runs",
            json=_provision_payload(deliveries=["d1", "d2"]),
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()["data"]
        assert data["first_delivery_id"] == "d1"
        assert data["delivery_ids"] == ["d1", "d2"]

    @pytest.mark.asyncio
    async def test_provision_without_batch_surfaces_empty_provenance(self) -> None:
        """Legacy provisioning (no batch) returns None/[] provenance fields."""
        conn = _mk_conn()
        conn.fetchrow = AsyncMock(
            side_effect=[_auth_row(), None, _mk_lifecycle_row()]
        )
        conn.fetch = AsyncMock(
            side_effect=[[mock_row({"afk_run_id": _RUN_ID})], []]
        )
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.post(
            "/api/v1/afk/executions/runs", json=_provision_payload()
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()["data"]
        assert data["first_delivery_id"] is None
        assert data["delivery_ids"] == []

    @pytest.mark.asyncio
    async def test_provision_rejects_empty_delivery_entries(self) -> None:
        """An empty string inside deliveries is rejected with 422."""
        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_auth_row())
        client = create_client(conn)

        resp = await client.post(
            "/api/v1/afk/executions/runs",
            json=_provision_payload(deliveries=["d1", ""]),
        )
        assert resp.status_code == 422


class TestExecutionBindingApiMultiplicity:
    """POST /api/v1/afk/executions — optional afk_run_id (issue #595)."""

    @pytest.mark.asyncio
    async def test_create_with_supplied_afk_run_id(self) -> None:
        """A callback with a pre-provisioned afk_run_id attaches to it (201)."""
        conn = _mk_conn()
        saved_row = _mk_binding_row(afk_run_id=_RUN_ID)
        # auth → existing binding (None) → run lookup → ownership check (None)
        # → re-read after insert
        conn.fetchrow = AsyncMock(
            side_effect=[
                _auth_row(),
                None,
                mock_row({"afk_run_id": _RUN_ID}),
                None,
                saved_row,
            ]
        )
        conn.fetch = AsyncMock(return_value=[mock_row({"id": uuid.uuid4()})])
        conn.execute = AsyncMock(return_value="UPDATE 1")
        client = create_client(conn)

        resp = await client.post(
            "/api/v1/afk/executions",
            json=_execution_payload(afk_run_id=_RUN_ID),
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["data"]["afk_run_id"] == _RUN_ID

    @pytest.mark.asyncio
    async def test_unknown_afk_run_id_returns_404(self) -> None:
        """A supplied afk_run_id referencing no lifecycle returns 404."""
        conn = _mk_conn()
        # auth → existing binding (None) → run lookup (None)
        conn.fetchrow = AsyncMock(side_effect=[_auth_row(), None, None])
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.post(
            "/api/v1/afk/executions",
            json=_execution_payload(afk_run_id="01HMISSING0000000000000001"),
        )
        assert resp.status_code == 404, resp.text
        assert "not found" in resp.json()["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_replay_with_different_afk_run_id_returns_409(self) -> None:
        """A replay supplying a different afk_run_id is rejected with 409."""
        conn = _mk_conn()
        existing = _mk_binding_row(
            awx_job_id=42, afk_run_id="01HOTHER000000000000000001", trigger_type="manual"
        )
        conn.fetchrow = AsyncMock(side_effect=[_auth_row(), existing])
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.post(
            "/api/v1/afk/executions",
            json=_execution_payload(afk_run_id=_RUN_ID),
        )
        assert resp.status_code == 409, resp.text

    @pytest.mark.asyncio
    async def test_legacy_replay_omitting_afk_run_id_returns_200(self) -> None:
        """A legacy replay (no afk_run_id) stays idempotent against a stored run."""
        conn = _mk_conn()
        existing = _mk_binding_row(awx_job_id=42, afk_run_id=_RUN_ID, trigger_type="manual")
        # auth → existing binding (replay) → re-read for the 200 response
        conn.fetchrow = AsyncMock(side_effect=[_auth_row(), existing, existing])
        conn.fetch = AsyncMock(return_value=[])
        conn.execute = AsyncMock()
        client = create_client(conn)

        resp = await client.post(
            "/api/v1/afk/executions", json=_execution_payload()
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["afk_run_id"] == _RUN_ID

    @pytest.mark.asyncio
    async def test_malformed_afk_run_id_returns_422(self) -> None:
        """A non-26-char afk_run_id is rejected by the schema (422)."""
        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=_auth_row())
        client = create_client(conn)

        resp = await client.post(
            "/api/v1/afk/executions", json=_execution_payload(afk_run_id="short")
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_write_with_afk_run_id_requires_collector_credential(self) -> None:
        """POST with afk_run_id but no collector credential returns 401/403."""
        conn = _mk_conn()
        conn.fetchrow = AsyncMock(return_value=None)
        client = create_client(conn, api_key=None)

        resp = await client.post(
            "/api/v1/afk/executions",
            json=_execution_payload(afk_run_id=_RUN_ID),
        )
        assert resp.status_code in (401, 403)


# ══════════════════════════════════════════════════════════════════════════
#  Migration 0040 — batch provenance (offline render + ORM mirror)
# ══════════════════════════════════════════════════════════════════════════


def _alembic_cfg():
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", "postgresql://none:none@localhost/none")
    return cfg


def _load_migration_module():
    """Load the 0040 migration module by file path (versions/ is not a package)."""
    spec = importlib.util.spec_from_file_location(
        "afk_run_batch_provenance_migration_0040", _MIGRATION_FILE
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_pre_existing_py39_migration_error(exc: BaseException) -> bool:
    """Detect the pre-existing 0024/0025 ``str | None`` import failure on 3.9."""
    return isinstance(exc, TypeError) and "unsupported operand type(s) for |" in str(exc)


def _render_delta_guarded(command: str, target: str) -> str:
    import alembic.command

    try:
        cfg = _alembic_cfg()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            getattr(alembic.command, command)(cfg, target, sql=True)
        return buf.getvalue()
    except BaseException as exc:  # noqa: BLE001 - re-raise unless pre-existing
        if _is_pre_existing_py39_migration_error(exc):
            pytest.skip(
                "Pre-existing Python 3.9 migration import failure "
                "(0024/0025 use `str | None` at module level); "
                "run on Python >=3.12 to exercise the offline render."
            )
        raise


class TestMigration0040:
    """The 0040 batch-provenance migration contract."""

    def test_migration_module_declares_revision_0040(self) -> None:
        module = _load_migration_module()
        assert module.revision == "0040"
        assert module.down_revision == "0039"

    def test_migration_module_imports_on_py39(self) -> None:
        """Importing the new migration must not trip the ``str | None`` 3.9 error."""
        _load_migration_module()

    def test_migration_module_exposes_reversible_upgrade_and_downgrade(self) -> None:
        module = _load_migration_module()
        assert callable(module.upgrade)
        assert callable(module.downgrade)

    def test_upgrade_adds_column_and_batch_table(self) -> None:
        sql = _render_delta_guarded("upgrade", "0039:0040")
        assert "ADD COLUMN first_delivery_id" in sql
        assert "CREATE TABLE afk_run_delivery_batches" in sql
        assert "uq_afk_run_delivery_batches_run_delivery" in sql

    def test_upgrade_is_additive_only(self) -> None:
        """The upgrade never drops columns, tables, or constraints."""
        sql = _render_delta_guarded("upgrade", "0039:0040")
        assert "DROP TABLE" not in sql
        assert "DROP COLUMN" not in sql
        assert "DROP CONSTRAINT" not in sql

    def test_downgrade_drops_batch_table_and_column(self) -> None:
        sql = _render_delta_guarded("downgrade", "0040:0039")
        assert "DROP TABLE afk_run_delivery_batches" in sql
        assert "DROP COLUMN first_delivery_id" in sql

    def test_orm_model_mirrors_batch_provenance(self) -> None:
        """The ORM mirrors the column, the table, and the unique constraint."""
        from app.db.models.afk import AFKRun, AFKRunDeliveryBatch

        column_names = {c.name for c in AFKRun.__table__.columns}
        assert "first_delivery_id" in column_names

        batch = AFKRunDeliveryBatch.__table__
        batch_columns = {c.name for c in batch.columns}
        for column in ("afk_run_id", "delivery_id", "position", "created_at"):
            assert column in batch_columns, f"batch table missing column: {column}"

        constraint_names = {c.name for c in batch.constraints}
        assert "uq_afk_run_delivery_batches_run_delivery" in constraint_names
