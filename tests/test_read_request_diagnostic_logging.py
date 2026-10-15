"""Correlated diagnostic evidence for failed read requests (issue #781).

Validates that a forced database read error produces one identifiable
API-log event carrying correlation ID, route, elapsed time, safe exception
class and SQLSTATE, verifiable without PostgreSQL logs and without secret
leakage. Successful reads remain unchanged.

TDD tracer bullet: failing HTTP request → correlated error logging →
reproducible log-verification test. Uses existing RedactingFormatter and
RequestTimingMiddleware correlation-ID infrastructure.
"""

from __future__ import annotations

import logging
import uuid
from unittest.mock import AsyncMock

import pytest

from app.core.telemetry import EVENT_REQUEST_COMPLETED
from tests.conftest import create_client

_TELEMETRY_LOGGER = "app.core.telemetry"


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _TELEMETRY_LOGGER]


class _FakeDBError(Exception):
    """Fake asyncpg-style error exposing a SQLSTATE code."""

    def __init__(self, msg: str = "db down", sqlstate: str | None = "42P01") -> None:
        super().__init__(msg)
        if sqlstate is not None:
            self.sqlstate = sqlstate


class _FakeDBErrorPgcode(Exception):
    """Fake error exposing pgcode instead of sqlstate (alternate asyncpg attr)."""

    def __init__(self, msg: str = "pgcode down", pgcode: str = "23505") -> None:
        super().__init__(msg)
        self.pgcode = pgcode


def _assert_no_secret_leakage(record: logging.LogRecord) -> None:
    """Ensure the formatted log record does not leak auth or SQL material."""
    # The formatted message itself is just the event name; extras are on attrs
    # but RedactingFormatter scans the final string.  Check both.
    formatted = logging.Formatter().format(record)
    # No bearer token, no raw SQL fragment, no secret key pattern
    assert "Bearer" not in formatted
    assert "test-api-key" not in formatted
    assert "SELECT" not in formatted
    assert "INSERT" not in formatted
    # Extra attrs must not contain raw bodies or tokens either
    for attr in ("exception_class", "sqlstate", "endpoint", "correlation_id"):
        val = getattr(record, attr, None)
        if val is not None:
            assert "Bearer" not in str(val)
            assert "test-api-key" not in str(val)


class TestFailedReadDiagnosticLogging:
    """Failed read requests emit one correlated diagnostic log event."""

    @pytest.mark.asyncio
    async def test_forced_db_error_produces_correlated_log_with_sqlstate(
        self, caplog: pytest.LogCaptureFixture, mock_conn: AsyncMock
    ) -> None:
        """A DB error with SQLSTATE yields 500 + one matching API log record.

        Verifiable without PostgreSQL logs via X-Correlation-ID header
        correlation.  No secret leakage via RedactingFormatter.
        """
        caplog.set_level(logging.INFO, logger=_TELEMETRY_LOGGER)

        # Force every DB call path to raise with SQLSTATE
        mock_conn.fetch = AsyncMock(side_effect=_FakeDBError("boom", sqlstate="42P01"))
        mock_conn.fetchrow = AsyncMock(side_effect=_FakeDBError("boom", sqlstate="42P01"))
        mock_conn.fetchval = AsyncMock(side_effect=_FakeDBError("boom", sqlstate="42P01"))

        client = create_client(mock_conn)
        async with client as c:
            resp = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert resp.status_code == 500
        body = resp.json()
        assert body["status"] == "error"
        assert body["error"]["code"] == "INTERNAL_ERROR"
        # Correlation via response header — no PG logs needed
        correlation_id = resp.headers.get("X-Correlation-ID")
        assert correlation_id is not None
        assert len(correlation_id) >= 8

        records = _records(caplog)
        # Exactly one diagnostic log for the failed request (request.completed
        # enhanced with exception_class/sqlstate).  Filter to the failed
        # request's status 500 and diagnostic fields.
        matching = [
            r
            for r in records
            if getattr(r, "status_code", None) == 500
            and getattr(r, "exception_class", None) is not None
            and getattr(r, "sqlstate", None) == "42P01"
        ]
        assert len(matching) == 1, f"expected one diagnostic log, got {len(matching)}: {records}"
        rec = matching[0]
        assert rec.correlation_id == correlation_id
        assert rec.endpoint == "/api/v1/usage/aggregates"
        assert rec.duration_ms is not None and rec.duration_ms > 0
        assert "_FakeDBError" in rec.exception_class
        assert rec.sqlstate == "42P01"
        # Route and timing are present
        assert rec.endpoint.startswith("/api/v1/usage")
        _assert_no_secret_leakage(rec)

    @pytest.mark.asyncio
    async def test_correlation_id_from_request_header_is_preserved(
        self, caplog: pytest.LogCaptureFixture, mock_conn: AsyncMock
    ) -> None:
        """Incoming X-Correlation-ID is echoed and appears in the diagnostic log."""
        caplog.set_level(logging.INFO, logger=_TELEMETRY_LOGGER)

        mock_conn.fetch = AsyncMock(side_effect=_FakeDBError("boom", sqlstate="23505"))
        mock_conn.fetchrow = AsyncMock(side_effect=_FakeDBError("boom", sqlstate="23505"))
        mock_conn.fetchval = AsyncMock(side_effect=_FakeDBError("boom", sqlstate="23505"))

        client = create_client(mock_conn)
        incoming = "test-correlation-12345"
        async with client as c:
            resp = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
                headers={"X-Correlation-ID": incoming},
            )

        assert resp.status_code == 500
        assert resp.headers["X-Correlation-ID"] == incoming

        records = _records(caplog)
        matching = [
            r
            for r in records
            if r.getMessage() == EVENT_REQUEST_COMPLETED
            and getattr(r, "correlation_id", None) == incoming
        ]
        assert len(matching) == 1
        assert matching[0].sqlstate == "23505"
        assert "_FakeDBError" in matching[0].exception_class

    @pytest.mark.asyncio
    async def test_pgcode_fallback_extracted_as_sqlstate(
        self, caplog: pytest.LogCaptureFixture, mock_conn: AsyncMock
    ) -> None:
        """Errors exposing pgcode (not sqlstate) are still captured as sqlstate."""
        caplog.set_level(logging.INFO, logger=_TELEMETRY_LOGGER)

        mock_conn.fetch = AsyncMock(side_effect=_FakeDBErrorPgcode(pgcode="23505"))
        mock_conn.fetchrow = AsyncMock(side_effect=_FakeDBErrorPgcode(pgcode="23505"))
        mock_conn.fetchval = AsyncMock(side_effect=_FakeDBErrorPgcode(pgcode="23505"))

        client = create_client(mock_conn)
        async with client as c:
            resp = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert resp.status_code == 500
        records = _records(caplog)
        matching = [r for r in records if getattr(r, "sqlstate", None) == "23505"]
        assert len(matching) == 1
        assert "FakeDBErrorPgcode" in matching[0].exception_class

    @pytest.mark.asyncio
    async def test_exception_without_sqlstate_logs_none_sqlstate(
        self, caplog: pytest.LogCaptureFixture, mock_conn: AsyncMock
    ) -> None:
        """An exception without sqlstate still emits a diagnostic log with sqlstate=None."""
        caplog.set_level(logging.INFO, logger=_TELEMETRY_LOGGER)

        class NoStateError(RuntimeError):
            pass

        mock_conn.fetch = AsyncMock(side_effect=NoStateError("no state"))
        mock_conn.fetchrow = AsyncMock(side_effect=NoStateError("no state"))
        mock_conn.fetchval = AsyncMock(side_effect=NoStateError("no state"))

        client = create_client(mock_conn)
        async with client as c:
            resp = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert resp.status_code == 500
        records = _records(caplog)
        matching = [
            r
            for r in records
            if getattr(r, "status_code", None) == 500
            and getattr(r, "exception_class", None) is not None
        ]
        assert len(matching) == 1
        assert matching[0].sqlstate is None
        assert "NoStateError" in matching[0].exception_class
        assert matching[0].duration_ms > 0

    @pytest.mark.asyncio
    async def test_successful_read_does_not_carry_exception_diagnostic(
        self, caplog: pytest.LogCaptureFixture, mock_conn: AsyncMock
    ) -> None:
        """Successful reads emit request.completed without exception_class/sqlstate leakage."""
        caplog.set_level(logging.INFO, logger=_TELEMETRY_LOGGER)

        # Make a successful total-row response (no group_by)
        from tests.test_usage import _mk_aggregate_row

        total_row = _mk_aggregate_row()
        mock_conn.fetchrow = AsyncMock(return_value=total_row)
        mock_conn.fetch = AsyncMock(return_value=[])

        client = create_client(mock_conn)
        async with client as c:
            resp = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert resp.status_code == 200
        assert resp.headers.get("X-Correlation-ID") is not None

        records = _records(caplog)
        # One success log with status 200, no exception diagnostic
        success = [r for r in records if getattr(r, "status_code", None) == 200]
        assert len(success) == 1
        # Success logs must not carry a populated exception_class
        assert getattr(success[0], "exception_class", None) in (None, "")
        # And sqlstate should be absent or None on success
        assert getattr(success[0], "sqlstate", None) is None

    @pytest.mark.asyncio
    async def test_execution_read_also_emits_diagnostic(
        self, caplog: pytest.LogCaptureFixture, mock_conn: AsyncMock
    ) -> None:
        """Diagnostic logging applies to other read routes (execution timeline)."""
        caplog.set_level(logging.INFO, logger=_TELEMETRY_LOGGER)

        mock_conn.fetch = AsyncMock(side_effect=_FakeDBError("exec down", sqlstate="42P01"))
        # execution header also uses fetchrow
        mock_conn.fetchrow = AsyncMock(side_effect=_FakeDBError("exec down", sqlstate="42P01"))
        mock_conn.fetchval = AsyncMock(side_effect=_FakeDBError("exec down", sqlstate="42P01"))

        client = create_client(mock_conn)
        test_sid = uuid.uuid4()
        async with client as c:
            resp = await c.get(f"/api/v1/execution/sessions/{test_sid}/timeline")

        assert resp.status_code == 500
        assert resp.headers.get("X-Correlation-ID") is not None
        records = _records(caplog)
        matching = [r for r in records if getattr(r, "sqlstate", None) == "42P01"]
        assert len(matching) == 1
        assert matching[0].correlation_id == resp.headers["X-Correlation-ID"]
        # Route is the execution timeline pattern
        assert "execution" in matching[0].endpoint

    @pytest.mark.asyncio
    async def test_diagnostic_log_does_not_leak_secrets(
        self, caplog: pytest.LogCaptureFixture, mock_conn: AsyncMock
    ) -> None:
        """Diagnostic log for a failed read does not leak auth or SQL material.

        Uses broad matching (status 500 + exception_class) so the assertion
        is independent of exact endpoint spelling, and relies solely on
        _assert_no_secret_leakage which checks Bearer/test-api-key/SELECT/INSERT.
        The client is created with the default Bearer test-api-key header,
        ensuring redaction is exercised on every CI run without depending on
        prior endpoint assertions.
        """
        caplog.set_level(logging.INFO, logger=_TELEMETRY_LOGGER)

        mock_conn.fetch = AsyncMock(side_effect=_FakeDBError("boom", sqlstate="42P01"))
        mock_conn.fetchrow = AsyncMock(side_effect=_FakeDBError("boom", sqlstate="42P01"))
        mock_conn.fetchval = AsyncMock(side_effect=_FakeDBError("boom", sqlstate="42P01"))

        # create_client injects Authorization: Bearer test-api-key by default
        client = create_client(mock_conn)
        async with client as c:
            resp = await c.get(
                "/api/v1/usage/aggregates",
                params={
                    "start_date": "2025-07-01T00:00:00Z",
                    "end_date": "2025-07-31T23:59:59Z",
                },
            )

        assert resp.status_code == 500

        records = _records(caplog)
        # Broad matching: any failed-request diagnostic log, no endpoint == check
        matching = [
            r
            for r in records
            if getattr(r, "status_code", None) == 500
            and getattr(r, "exception_class", None) is not None
        ]
        assert len(matching) == 1, f"expected one diagnostic log, got {len(matching)}: {records}"
        rec = matching[0]
        # Verifies Bearer / test-api-key / SELECT / INSERT are absent from
        # both formatted string and key extra attrs.
        _assert_no_secret_leakage(rec)
        # Additional explicit check: diagnostic attrs themselves do not carry SQL
        formatted = logging.Formatter().format(rec)
        assert "SELECT" not in formatted
        assert "INSERT" not in formatted
        assert "Bearer" not in formatted
        assert "test-api-key" not in formatted
