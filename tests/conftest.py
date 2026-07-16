"""Shared test fixtures and helper builders for the OpenCode Gateway test suite.

Centralises duplicated mock rows, sessions, clients, and helper builders
that were previously copy-pasted across individual test files.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from app.core.factory import create_app
from app.db.session import get_session

# ══════════════════════════════════════════════════════════════════════════
#  Test API key — set before any module imports create_app() so that the
#  auth middleware passes for all existing tests.  Individual auth tests
#  override this by creating clients without the header.
# ══════════════════════════════════════════════════════════════════════════

_TEST_API_KEY = "test-api-key"
os.environ.setdefault("GATEWAY_API_KEY", _TEST_API_KEY)

# ══════════════════════════════════════════════════════════════════════════
#  Core helpers
# ══════════════════════════════════════════════════════════════════════════


def mock_row(data: dict) -> MagicMock:
    """Return a MagicMock that behaves like an asyncpg Record for dict-like access."""
    row = MagicMock()
    row.__getitem__.side_effect = data.__getitem__
    row.get.side_effect = data.get
    return row


def create_client(
    mock_conn: AsyncMock,
    *,
    api_key: str | None = _TEST_API_KEY,
) -> AsyncClient:
    """Build app with overridden dependencies, return httpx AsyncClient.

    By default adds an ``Authorization: Bearer <api_key>`` header so
    existing tests pass through the API-key middleware.  Pass
    ``api_key=None`` to create an unauthenticated client (for auth
    failure tests).
    """
    app = create_app(configure_logging=False)
    mock_pool = AsyncMock()
    mock_pool.pool = None
    app.state.pool = mock_pool

    async def _override_get_session(request: Request):
        yield mock_conn

    app.dependency_overrides[get_session] = _override_get_session

    headers: dict[str, str] = {}
    if api_key is not None:
        headers["Authorization"] = f"Bearer {api_key}"

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test", headers=headers)


# ══════════════════════════════════════════════════════════════════════════
#  Fixtures
# ══════════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _skip_migrations_in_unit_tests() -> None:
    """Replace ensure_schema with a no-op in unit tests (no real database).

    Unit tests use mock database connections — Alembic cannot run real
    migrations against them.  Individual test files that need to verify
    ensure_schema behaviour (e.g. test_schema.py) apply their own
    targeted patches inside the test function body.
    """
    from unittest.mock import patch

    with patch("app.core.factory.ensure_schema", AsyncMock()):
        yield


@pytest.fixture
def mock_conn() -> AsyncMock:
    """Return a mock asyncpg connection."""
    from unittest.mock import MagicMock

    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    mock_tx = AsyncMock()
    mock_tx.__aenter__ = AsyncMock(return_value=mock_tx)
    mock_tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=mock_tx)
    return conn


@pytest.fixture
def client(mock_conn: AsyncMock) -> AsyncClient:
    """Build app with overridden get_session dependency, return httpx AsyncClient."""
    return create_client(mock_conn)
