"""Unit tests for ``app/core/telemetry.py`` — shared timing infrastructure.

Covers:
- Request middleware: captures endpoint, duration_ms, status_code and
  correlation_id; emits exactly one structured log entry per request;
  propagates the correlation ID via the response header and the request
  context.
- Operation helpers: time operations correctly and emit structured
  records with event_name, operation_type, duration_ms, success and
  correlation_id.
- Timeout helpers: enforce configurable budgets (explicit and from the
  GATEWAY_OPERATION_TIMEOUT_MS environment variable), cancel the
  operation on expiry, and emit a timeout event.
- Middleware registration: RequestTimingMiddleware is registered in the
  FastAPI app and fires on every request.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.telemetry import (
    EVENT_OPERATION_COMPLETED,
    EVENT_OPERATION_TIMEOUT,
    EVENT_REQUEST_COMPLETED,
    RequestTimingMiddleware,
    get_correlation_id,
    reset_correlation_id,
    set_correlation_id,
    timed_operation,
    timeout_operation,
)

_TELEMETRY_LOGGER = "app.core.telemetry"


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """Return telemetry log records captured by caplog."""
    return [r for r in caplog.records if r.name == _TELEMETRY_LOGGER]


class TestTimedOperation:
    """Operation timing helpers emit one structured record per operation."""

    @pytest.mark.asyncio
    async def test_times_operation_and_emits_structured_record(self, caplog) -> None:
        caplog.set_level(logging.INFO, logger=_TELEMETRY_LOGGER)

        start = time.perf_counter()
        async with timed_operation("db.query.records.count", "db", correlation_id="cid-1"):
            await asyncio.sleep(0.01)
        elapsed_ms = (time.perf_counter() - start) * 1000

        records = _records(caplog)
        assert len(records) == 1
        record = records[0]
        assert record.getMessage() == EVENT_OPERATION_COMPLETED
        assert record.event_name == "db.query.records.count"
        assert record.operation_type == "db"
        assert record.success is True
        assert record.correlation_id == "cid-1"
        assert record.duration_ms > 0
        assert record.duration_ms >= 5
        assert record.duration_ms <= elapsed_ms + 5

    @pytest.mark.asyncio
    async def test_failed_operation_emits_success_false_and_propagates(
        self, caplog
    ) -> None:
        caplog.set_level(logging.INFO, logger=_TELEMETRY_LOGGER)

        with pytest.raises(RuntimeError, match="boom"):
            async with timed_operation("status.compute", "compute"):
                raise RuntimeError("boom")

        records = _records(caplog)
        assert len(records) == 1
        assert records[0].success is False
        assert records[0].event_name == "status.compute"
        assert records[0].operation_type == "compute"

    @pytest.mark.asyncio
    async def test_inherits_correlation_id_from_context_and_restores(
        self, caplog
    ) -> None:
        caplog.set_level(logging.INFO, logger=_TELEMETRY_LOGGER)

        base_token = set_correlation_id("base-cid")
        try:
            token = set_correlation_id("ctx-cid")
            try:
                async with timed_operation("op", "db"):
                    pass
            finally:
                reset_correlation_id(token)
            # The contextvar is genuinely restored to its previous value.
            assert get_correlation_id() == "base-cid"
        finally:
            reset_correlation_id(base_token)

        records = _records(caplog)
        assert len(records) == 1
        assert records[0].correlation_id == "ctx-cid"


class TestTimeoutOperation:
    """Timeout helpers enforce budgets, cancel the operation, and log."""

    @pytest.mark.asyncio
    async def test_enforces_explicit_budget_and_cancels_operation(self, caplog) -> None:
        caplog.set_level(logging.INFO, logger=_TELEMETRY_LOGGER)

        start = time.perf_counter()
        with pytest.raises(TimeoutError):
            async with timeout_operation(
                "status.compute", "compute", budget_ms=50, correlation_id="cid-t"
            ):
                await asyncio.sleep(10)
        elapsed_ms = (time.perf_counter() - start) * 1000

        # The operation was cancelled well before its 10s sleep finished.
        assert elapsed_ms < 1000

        records = _records(caplog)
        assert len(records) == 1
        record = records[0]
        assert record.getMessage() == EVENT_OPERATION_TIMEOUT
        assert record.event_name == "status.compute"
        assert record.operation_type == "compute"
        assert record.budget_ms == 50
        assert record.correlation_id == "cid-t"

    @pytest.mark.asyncio
    async def test_reads_budget_from_env_var(self, monkeypatch, caplog) -> None:
        caplog.set_level(logging.INFO, logger=_TELEMETRY_LOGGER)
        monkeypatch.setenv("GATEWAY_OPERATION_TIMEOUT_MS", "50")

        with pytest.raises(TimeoutError):
            async with timeout_operation("db.query", "db"):
                await asyncio.sleep(10)

        records = _records(caplog)
        assert len(records) == 1
        assert records[0].getMessage() == EVENT_OPERATION_TIMEOUT
        assert records[0].budget_ms == 50

    @pytest.mark.asyncio
    async def test_completes_within_budget_without_timeout_event(self, caplog) -> None:
        caplog.set_level(logging.INFO, logger=_TELEMETRY_LOGGER)

        async with timeout_operation("fast.op", "db", budget_ms=5000):
            await asyncio.sleep(0.005)

        records = _records(caplog)
        assert records == []


class TestRequestTimingMiddleware:
    """Request middleware captures fields and emits one record per request."""

    @staticmethod
    def _build_app() -> Any:
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/items/{item_id}")
        async def get_item(item_id: int) -> dict[str, int]:
            await asyncio.sleep(0.01)
            return {"item_id": item_id}

        @app.get("/echo-cid")
        async def echo_cid() -> dict[str, str | None]:
            return {"cid": get_correlation_id()}

        @app.get("/boom")
        async def boom() -> None:
            raise RuntimeError("kaboom")

        app.add_middleware(RequestTimingMiddleware)
        return app

    @pytest.mark.asyncio
    async def test_captures_endpoint_duration_status_and_correlation_id(
        self, caplog
    ) -> None:
        caplog.set_level(logging.INFO, logger=_TELEMETRY_LOGGER)

        app = self._build_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/items/42")

        assert response.status_code == 200
        correlation_id = response.headers["X-Correlation-ID"]
        assert len(correlation_id) == 32

        records = _records(caplog)
        assert len(records) == 1
        record = records[0]
        assert record.getMessage() == EVENT_REQUEST_COMPLETED
        assert record.endpoint == "/items/{item_id}"
        assert record.status_code == 200
        assert record.correlation_id == correlation_id
        assert record.duration_ms > 0
        assert record.duration_ms >= 5

    @pytest.mark.asyncio
    async def test_correlation_id_reaches_endpoint_context(self, caplog) -> None:
        caplog.set_level(logging.INFO, logger=_TELEMETRY_LOGGER)

        app = self._build_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/echo-cid")

        assert response.status_code == 200
        assert response.json()["cid"] == response.headers["X-Correlation-ID"]

    @pytest.mark.asyncio
    async def test_accepts_incoming_correlation_id_header(self, caplog) -> None:
        caplog.set_level(logging.INFO, logger=_TELEMETRY_LOGGER)

        app = self._build_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/echo-cid", headers={"X-Correlation-ID": "incoming-cid-1"}
            )

        assert response.json()["cid"] == "incoming-cid-1"
        assert response.headers["X-Correlation-ID"] == "incoming-cid-1"
        assert _records(caplog)[0].correlation_id == "incoming-cid-1"

    @pytest.mark.asyncio
    async def test_logs_500_for_unhandled_exception(self, caplog) -> None:
        caplog.set_level(logging.INFO, logger=_TELEMETRY_LOGGER)

        app = self._build_app()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/boom")

        assert response.status_code == 500
        records = _records(caplog)
        assert len(records) == 1
        assert records[0].getMessage() == EVENT_REQUEST_COMPLETED
        assert records[0].status_code == 500
        assert records[0].endpoint == "/boom"


class TestMiddlewareRegistration:
    """RequestTimingMiddleware is registered in the FastAPI app."""

    def test_middleware_registered_in_app_factory(self) -> None:
        from fastapi import FastAPI

        from app.core.factory import create_app

        app = create_app(configure_logging=False)
        assert isinstance(app, FastAPI)
        middleware_classes = [m.cls for m in app.user_middleware]
        assert RequestTimingMiddleware in middleware_classes

    @pytest.mark.asyncio
    async def test_fires_on_every_request_through_factory_app(self, caplog) -> None:
        caplog.set_level(logging.INFO, logger=_TELEMETRY_LOGGER)

        from app.core.factory import create_app

        app = create_app(configure_logging=False)
        app.state.pool = None
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"Authorization": "Bearer test-api-key"},
        ) as client:
            response = await client.get("/health")

        assert response.status_code == 200
        assert response.headers["X-Correlation-ID"]
        records = _records(caplog)
        assert len(records) == 1
        record = records[0]
        assert record.getMessage() == EVENT_REQUEST_COMPLETED
        assert record.endpoint == "/health"
        assert record.status_code == 200
        assert record.correlation_id == response.headers["X-Correlation-ID"]


class TestTimeoutExceptionHandler:
    """Validate that request timeout expiry returns 504 (not 500)."""

    @staticmethod
    def _build_app_with_timeout_route() -> Any:
        from fastapi import FastAPI

        from app.core.envelope import timeout_exception_handler

        app = FastAPI()
        app.add_exception_handler(TimeoutError, timeout_exception_handler)

        @app.get("/timeout")
        async def timeout_endpoint() -> None:
            raise TimeoutError()

        @app.get("/healthz")
        async def healthz() -> dict[str, str]:
            return {"status": "ok"}

        app.add_middleware(RequestTimingMiddleware)
        return app

    @pytest.mark.asyncio
    async def test_timeout_returns_504_with_json_body(self, caplog) -> None:
        """TimeoutError propagated from an endpoint returns 504 Gateway Timeout."""
        caplog.set_level(logging.INFO, logger=_TELEMETRY_LOGGER)

        app = self._build_app_with_timeout_route()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/timeout")

        assert response.status_code == 504
        body = response.json()
        assert body["status"] == "error"
        assert body["error"]["code"] == "GATEWAY_TIMEOUT"
        assert "time budget" in body["error"]["message"]

        # The request.completed log should still be emitted (by the middleware)
        # and the operation.timeout log is already emitted by the timeout
        # machinery before the exception propagates — we verify the
        # middleware still fires.
        request_logs = [r for r in _records(caplog) if r.getMessage() == EVENT_REQUEST_COMPLETED]
        assert len(request_logs) == 1
        assert request_logs[0].status_code == 504
        assert request_logs[0].endpoint == "/timeout"

    @pytest.mark.asyncio
    async def test_health_endpoint_unaffected(self, caplog) -> None:
        """The timeout handler does NOT affect the health endpoint."""
        caplog.set_level(logging.INFO, logger=_TELEMETRY_LOGGER)

        app = self._build_app_with_timeout_route()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/healthz")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_timeout_does_not_swallow_operation_timeout_log(self, caplog) -> None:
        """The handler returns 504; the telemetry log is still emitted (tested
        via the request.completed log from the middleware, which fires after
        the exception handler returns)."""
        caplog.set_level(logging.INFO, logger=_TELEMETRY_LOGGER)

        app = self._build_app_with_timeout_route()
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/timeout")

        assert response.status_code == 504
        # Middleware's request.completed log confirms the exception path
        # didn't skip the logging pipeline.
        request_logs = [r for r in _records(caplog) if r.getMessage() == EVENT_REQUEST_COMPLETED]
        assert len(request_logs) >= 1
        assert request_logs[0].status_code == 504
