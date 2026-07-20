"""End-to-end smoke test for the local same-origin Aurora Glass stack.

Starts the full Docker Compose stack (gateway + frontend + postgres),
verifies that the browser entrypoint serves Aurora Glass, and verifies
that Gateway API requests succeed through the same entrypoint (same-origin
proxy).  Tears down the stack after all checks pass.

Usage
-----
Run from the repository root with Docker and Docker Compose available:

    docker compose -f docker-compose.yaml -f docker-compose.smoke.yml up -d
    pytest tests/test_smoke_local_stack.py -v

Or rely on the ``stack_url`` fixture which handles the lifecycle:

    pytest tests/test_smoke_local_stack.py -v

The test skips itself if ``docker compose`` is not installed.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yaml"
SMOKE_OVERRIDE = REPO_ROOT / "docker-compose.smoke.yml"
FRONTEND_PORT = int(os.environ.get("FRONTEND_PORT", "8080"))
ENTRYPOINT = f"http://localhost:{FRONTEND_PORT}"

STACK_TIMEOUT = 120       # maximum seconds to wait for stack readiness
POLL_INTERVAL = 5         # seconds between health-poll attempts
HEALTHCHECK_RETRIES = 3   # number of consecutive 200s required for readiness


def _docker_compose_available() -> bool:
    """Return True if ``docker compose`` is installed and responsive."""
    try:
        result = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _stack_up(env: dict[str, str]) -> None:
    """Start the stack with both compose files."""
    subprocess.run(
        [
            "docker", "compose",
            "-f", str(COMPOSE_FILE),
            "-f", str(SMOKE_OVERRIDE),
            "up", "-d",
        ],
        check=True,
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )


def _stack_down(env: dict[str, str]) -> None:
    """Tear down the stack and remove volumes."""
    subprocess.run(
        [
            "docker", "compose",
            "-f", str(COMPOSE_FILE),
            "-f", str(SMOKE_OVERRIDE),
            "down", "-v",
        ],
        check=False,  # best effort during cleanup
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )


def _wait_for_stack(url: str, timeout: int = STACK_TIMEOUT) -> None:
    """Poll *url* until it returns 200 on consecutive attempts.

    Raises ``TimeoutError`` if the stack isn't healthy within *timeout*
    seconds.
    """
    start = time.monotonic()
    successes = 0

    while time.monotonic() - start < timeout:
        try:
            response = httpx.get(url, timeout=5)
            if response.status_code == 200:
                successes += 1
                if successes >= HEALTHCHECK_RETRIES:
                    return
            else:
                successes = 0
        except (httpx.ConnectError, httpx.TimeoutException):
            successes = 0

        time.sleep(POLL_INTERVAL)

    raise TimeoutError(
        f"Stack did not become healthy within {timeout}s "
        f"(last checked {url})"
    )


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def stack_url() -> str:
    """Start the full Docker Compose stack, wait for readiness, yield the
    frontend entrypoint URL, and tear down when the module finishes.

    Skips the test if ``docker compose`` is not available.
    """
    if not _docker_compose_available():
        pytest.skip("docker compose is not installed — cannot run smoke test")

    # Ensure GATEWAY_ENV is set for the shell (used by docker-compose.smoke.yml)
    env = os.environ.copy()
    env.setdefault("GATEWAY_ENV", "development")

    _stack_up(env)
    try:
        _wait_for_stack(ENTRYPOINT)
        yield ENTRYPOINT
    finally:
        _stack_down(env)


# ── Tests ─────────────────────────────────────────────────────────────────


class TestSmokeSameOriginStack:
    """End-to-end smoke tests for the local same-origin Aurora Glass stack.

    All tests use the same module-scoped ``stack_url`` fixture so the stack
    is started once and torn down after the last test.
    """

    def test_entrypoint_serves_aurora_glass(self, stack_url: str) -> None:
        """GET / should return HTML that contains the Aurora Glass title."""
        response = httpx.get(f"{stack_url}/", timeout=10)
        assert response.status_code == 200, (
            f"Expected 200 from entrypoint, got {response.status_code}"
        )
        assert "Aurora Glass" in response.text, (
            "Response body should contain 'Aurora Glass'"
        )
        content_type = response.headers.get("content-type", "")
        assert "text/html" in content_type, (
            f"Expected text/html content type, got {content_type}"
        )

    def test_health_endpoint_through_same_origin(self, stack_url: str) -> None:
        """GET /health through the frontend proxy should return a 200 JSON response
        with status 'ok'."""
        response = httpx.get(f"{stack_url}/health", timeout=10)
        assert response.status_code == 200, (
            f"Expected 200 from /health proxy, got {response.status_code}"
        )
        payload = response.json()
        assert payload.get("status") == "ok", (
            f"Expected status 'ok', got {payload.get('status')!r}"
        )
        # The response-envelope middleware wraps the health data in a "data" field.
        # When the database is unreachable (no Postgres), "database" will show
        # "disconnected" — that's acceptable; we're testing the proxy works.
        assert "data" in payload, (
            "Response should contain a 'data' envelope"
        )

    def test_openapi_schema_through_same_origin(self, stack_url: str) -> None:
        """The OpenAPI schema served through the frontend proxy should return
        valid JSON (``/openapi.json`` is proxied by nginx to the Gateway).
        """
        response = httpx.get(
            f"{stack_url}/openapi.json",
            timeout=10,
        )
        # The proxy works if we get a valid JSON response (200) from the
        # Gateway.  In development mode with no API key, this should succeed.
        assert response.status_code == 200, (
            f"Expected 200 from /openapi.json proxy, got {response.status_code} "
            f"with body: {response.text[:300]}"
        )
        schema = response.json()
        assert "openapi" in schema, (
            "OpenAPI response should contain an 'openapi' version field"
        )
        assert "info" in schema, (
            "OpenAPI response should contain an 'info' field"
        )
        assert schema["info"].get("title") == "OpenCode Gateway", (
            f"Expected title 'OpenCode Gateway', got {schema['info'].get('title')!r}"
        )
