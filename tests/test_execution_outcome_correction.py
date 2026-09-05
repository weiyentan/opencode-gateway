# ruff: noqa: UP017 — timezone.utc for py39 compat; datetime.UTC is 3.11+
"""Tests for the execution-outcome correction script (issue #654).

PR #653 (AWX job 9293) was wrongly recorded as ``outcome = 'cancelled'``
because a prose-substring heuristic in the AWX playbooks matched
"cancell" in the coordinator's closing summary.  The API layer can never
repair this — terminal rows are never overwritten (repository contract,
issue #590) — so the correction is an auditable, least-privilege operator
script: ``scripts/correct_execution_outcome.py``.

Design (issue #654 decision, option 3):

1. **Auditable** — every correction writes a row to the
   ``execution_outcome_corrections`` audit table (migration 0043) in the
   same transaction as the flip: previous/new outcome, previous failure
   metadata, the operator reason, and the correction time.
2. **Narrow** — only ``cancelled`` rows may be corrected (to
   ``completed``); ``running`` / ``failed`` / already-correct rows are
   refused or no-ops.  Failure metadata is cleared on correction (a
   ``completed`` execution carries no Failure Summary).
3. **Verifiable** — an ``--audit`` read-only mode lists every
   ``cancelled`` execution with its failure metadata so the operator can
   verify whether the ``cancelled`` entries for change-requests #619 and
   #608 stem from the same prose-substring root cause.
4. **Idempotent** — re-running on a corrected row is a no-op.

Tests follow the mock pattern from ``tests/test_usage.py``
(SQL-content assertions + AsyncMock connection).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+


def _binding_row(**overrides) -> dict:
    """A scan row as returned by the correction script's SELECT."""
    row = {
        "id": uuid.uuid4(),
        "awx_job_id": 9293,
        "outcome": "cancelled",
        "entity_number": "653",
        "title": "Fix execution binding outcome",
        "failure_reason": "Cancelled — job stopped",
        "failure_summary": "Run ended; summary mentioned 'cancelled' in prose",
        "finished_at": datetime(2026, 9, 1, tzinfo=UTC),
    }
    row.update(overrides)
    return row


class TestListCancelledExecutions:
    """The read-only audit surface for issue #654 point 2."""

    @pytest.mark.asyncio
    async def test_lists_cancelled_rows_with_failure_metadata(self):
        """_list_cancelled_executions returns every cancelled row with the
        fields needed to identify prose-substring false cancellations."""
        from scripts.correct_execution_outcome import _list_cancelled_executions

        rows = [
            _binding_row(awx_job_id=9293, entity_number="653"),
            _binding_row(awx_job_id=9300, entity_number="619"),
        ]
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=rows)
        result = await _list_cancelled_executions(mock_conn)
        assert len(result) == 2
        assert result[0]["awx_job_id"] == 9293
        assert result[1]["entity_number"] == "619"
        sql = mock_conn.fetch.call_args[0][0]
        # Reads only the execution_bindings audit surface; filters cancelled.
        assert "execution_bindings" in sql
        assert "cancelled" in sql
        assert "failure_summary" in sql

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_none_cancelled(self):
        from scripts.correct_execution_outcome import _list_cancelled_executions

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        result = await _list_cancelled_executions(mock_conn)
        assert result == []


class TestCorrectExecutionOutcome:
    """The guarded correction path for issue #654 point 1 (option 3)."""

    @pytest.mark.asyncio
    async def test_corrects_cancelled_to_completed_with_audit_row(self):
        """A cancelled row flips to completed, clears failure metadata,
        and writes the audit trail row in the same transaction."""
        from scripts.correct_execution_outcome import _correct

        stored = _binding_row()
        mock_conn = AsyncMock()
        mock_conn.fetchrow = AsyncMock(return_value=stored)
        mock_conn.transaction = MagicMock()
        status = await _correct(
            mock_conn, awx_job_id=9293, reason="False cancellation: prose heuristic"
        )
        assert status == "corrected"

        # Two writes inside the transaction: the audit row and the flip.
        execute_sqls = [c[0][0] for c in mock_conn.execute.call_args_list]
        assert any("execution_outcome_corrections" in s for s in execute_sqls)
        assert any(
            "UPDATE execution_bindings" in s and "outcome = 'completed'" in s for s in execute_sqls
        )

        # The audit row carries the previous outcome and failure metadata.
        audit_call = next(
            c
            for c in mock_conn.execute.call_args_list
            if "execution_outcome_corrections" in c[0][0]
        )
        audit_args = audit_call[0]
        assert stored["id"] in audit_args  # binding id
        assert "cancelled" in audit_args  # previous outcome
        assert "completed" in audit_args  # new outcome
        assert stored["failure_reason"] in audit_args
        assert stored["failure_summary"] in audit_args

    @pytest.mark.asyncio
    async def test_correction_clears_failure_metadata(self):
        """The UPDATE statement sets failure_reason/failure_summary to NULL —
        a completed execution carries no Failure Summary."""
        from scripts.correct_execution_outcome import _correct

        mock_conn = AsyncMock()
        mock_conn.transaction = MagicMock()
        mock_conn.fetchrow = AsyncMock(return_value=_binding_row())
        await _correct(mock_conn, awx_job_id=9293, reason="operator reason")
        update_sql = next(
            c[0][0]
            for c in mock_conn.execute.call_args_list
            if "UPDATE execution_bindings" in c[0][0]
        )
        assert "failure_reason = NULL" in update_sql
        assert "failure_summary = NULL" in update_sql

    @pytest.mark.asyncio
    async def test_not_found(self):
        from scripts.correct_execution_outcome import _correct

        mock_conn = AsyncMock()
        mock_conn.transaction = MagicMock()
        mock_conn.fetchrow = AsyncMock(return_value=None)
        status = await _correct(mock_conn, awx_job_id=1, reason="r")
        assert status == "not_found"
        assert mock_conn.execute.call_count == 0

    @pytest.mark.asyncio
    async def test_refuses_running_row(self):
        """A running row is refused — the two-phase lifecycle owns that
        transition, never the correction script."""
        from scripts.correct_execution_outcome import _correct

        mock_conn = AsyncMock()
        mock_conn.transaction = MagicMock()
        mock_conn.fetchrow = AsyncMock(return_value=_binding_row(outcome="running"))
        status = await _correct(mock_conn, awx_job_id=1, reason="r")
        assert status == "refused"
        assert mock_conn.execute.call_count == 0

    @pytest.mark.asyncio
    async def test_refuses_failed_row(self):
        """A failed row is refused — different root-cause class."""
        from scripts.correct_execution_outcome import _correct

        mock_conn = AsyncMock()
        mock_conn.transaction = MagicMock()
        mock_conn.fetchrow = AsyncMock(return_value=_binding_row(outcome="failed"))
        status = await _correct(mock_conn, awx_job_id=1, reason="r")
        assert status == "refused"
        assert mock_conn.execute.call_count == 0

    @pytest.mark.asyncio
    async def test_already_completed_is_noop(self):
        """Re-running on a corrected row is an idempotent no-op."""
        from scripts.correct_execution_outcome import _correct

        mock_conn = AsyncMock()
        mock_conn.transaction = MagicMock()
        mock_conn.fetchrow = AsyncMock(return_value=_binding_row(outcome="completed"))
        status = await _correct(mock_conn, awx_job_id=9293, reason="r")
        assert status == "already_completed"
        assert mock_conn.execute.call_count == 0

    @pytest.mark.asyncio
    async def test_requires_reason_for_audit_trail(self):
        from scripts.correct_execution_outcome import _correct

        with pytest.raises(ValueError):
            await _correct(mock_conn := AsyncMock(), awx_job_id=9293, reason="   ")
        assert mock_conn.fetchrow.call_count == 0

    @pytest.mark.asyncio
    async def test_reason_bounded_to_1000_chars(self):
        from scripts.correct_execution_outcome import _correct

        with pytest.raises(ValueError):
            await _correct(mock_conn := AsyncMock(), awx_job_id=9293, reason="x" * 1001)
        assert mock_conn.fetchrow.call_count == 0

    @pytest.mark.asyncio
    async def test_correction_writes_within_a_transaction(self):
        """The audit row and the flip are atomic — one transaction."""
        from scripts.correct_execution_outcome import _correct

        mock_conn = AsyncMock()
        mock_conn.transaction = MagicMock()
        mock_conn.fetchrow = AsyncMock(return_value=_binding_row())
        await _correct(mock_conn, awx_job_id=9293, reason="r")
        assert mock_conn.transaction.call_count == 1


class TestCli:
    """Argument parsing and the read-only audit output."""

    def test_audit_command_parses(self):
        from scripts.correct_execution_outcome import _parse_args

        args = _parse_args(["audit"])
        assert args.command == "audit"

    def test_correct_command_requires_awx_job_id_and_reason(self):
        from scripts.correct_execution_outcome import _parse_args

        with pytest.raises(SystemExit):
            _parse_args(["correct"])

        args = _parse_args(["correct", "--awx-job-id", "9293", "--reason", "because"])
        assert args.command == "correct"
        assert args.awx_job_id == "9293"
        assert args.reason == "because"

    @pytest.mark.asyncio
    async def test_main_audit_mode_is_read_only(self, capsys):
        """`main(['audit'])` lists cancelled rows and exits 0 without writes."""
        from scripts.correct_execution_outcome import main

        captured = {}

        class _Conn:
            async def fetch(self, sql):
                captured["sql"] = sql
                return [_binding_row(awx_job_id=9293, entity_number="653")]

        class _FakePool:
            def acquire(self):
                conn = _Conn()

                class _Ctx:
                    async def __aenter__(self):
                        return conn

                    async def __aexit__(self, *exc):
                        return False

                return _Ctx()

            async def close(self):
                pass

        import scripts.correct_execution_outcome as script

        async def fake_get_pool():
            return _FakePool()

        original = script._get_pool
        script._get_pool = fake_get_pool
        try:
            rc = await main(["audit"])
        finally:
            script._get_pool = original
        assert rc == 0
        assert "cancelled" in captured["sql"]
        out = capsys.readouterr().out
        assert "9293" in out


class TestMigration0043:
    """The audit table migration — additive-only, reversible (issue #654)."""

    def _migration_path(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        return root / "alembic" / "versions" / "0043_execution_outcome_corrections.py"

    def _load_migration_module(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("migration_0043", self._migration_path())
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_revision_identifiers(self):
        module = self._load_migration_module()
        assert module.revision == "0043"
        assert module.down_revision == "0042"

    def test_upgrade_creates_audit_table(self):
        """The upgrade renders a CREATE TABLE for the audit table with the
        full before/after story (issue #654: auditable correction)."""
        import contextlib
        import io
        from pathlib import Path

        from alembic.command import upgrade
        from alembic.config import Config

        root = Path(__file__).resolve().parent.parent
        cfg = Config()
        cfg.set_main_option("script_location", str(root / "alembic"))
        cfg.set_main_option("sqlalchemy.url", "postgresql://none:none@localhost/none")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            upgrade(cfg, "0042:0043", sql=True)
        sql = buf.getvalue()
        assert "execution_outcome_corrections" in sql
        for column in (
            "execution_binding_id",
            "awx_job_id",
            "previous_outcome",
            "new_outcome",
            "previous_failure_reason",
            "previous_failure_summary",
            "reason",
            "corrected_at",
        ):
            assert column in sql
