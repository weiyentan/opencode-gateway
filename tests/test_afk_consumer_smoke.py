"""Deployment smoke test for the AFK outcome consumer (issue #531).

Starts the Compose stack's Postgres, Kafka, and AFK outcome consumer
containers, applies the database migrations, produces one normalized
provider event onto the configured topic (``engineering.events.normalized``),
and verifies the consumer ingests it into ``engineering_events`` — proving
the deployed topic configuration actually reaches the consumer's
subscription end to end.

Like ``tests/test_smoke_local_stack.py``, this test skips itself when
Docker Compose or the Docker daemon is unavailable, and it is marked
``integration`` so it is excluded from the default CI run
(``pytest -m "not integration"``).

Usage (from the repository root):

    pytest tests/test_afk_consumer_smoke.py -v -m integration
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import asyncpg
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yaml"

CONSUMER_CONTAINER = "opencode-afk-outcomes-consumer"
KAFKA_CONTAINER = "opencode-gateway-kafka"

SERVICES = ["postgres", "kafka", "afk-outcomes-consumer"]

POSTGRES_USER = os.environ.get("POSTGRES_USER", "opencode")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "opencode")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "opencode_gateway")

TOPIC = "engineering.events.normalized"
REPOSITORY = "github.com/octocat/Hello-World"
DELIVERY_ID = "smoke-issue-531-delivery"
EXTERNAL_ID = "531001"

WAIT_TIMEOUT = 300  # maximum seconds to wait for readiness/ingestion
POLL_INTERVAL = 3   # seconds between polls


def _docker_compose_available() -> bool:
    """Return True if Compose is installed and the Docker daemon is ready."""
    try:
        compose_result = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if compose_result.returncode != 0:
            return False

        daemon_result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return daemon_result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _compose(
    *args: str,
    env: dict[str, str] | None = None,
    check: bool = True,
    input: str | None = None,
) -> subprocess.CompletedProcess:
    """Run ``docker compose -f docker-compose.yaml <args>`` from the repo root."""
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],
        check=check,
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
        input=input,
    )


def _normalized_event() -> str:
    """One producer-contract-conforming v1 normalized ``issue.opened`` event."""
    payload = {
        "schema_version": "1.0",
        "event_type": "normalized",
        "provider": "github",
        "delivery_id": DELIVERY_ID,
        "resource": {
            "type": "issue",
            "repository_url": "https://github.com/octocat/Hello-World",
            "number": int(EXTERNAL_ID),
        },
        "action": "opened",
        "occurred_at": "2026-08-20T10:00:00Z",
        "ingested_at": "2026-08-20T10:00:01Z",
        "actor": "smoke-test",
        "redacted_payload": {
            "reference": {"provider": "github", "delivery_id": DELIVERY_ID}
        },
    }
    return json.dumps(payload)


def _wait_until(predicate, timeout: int = WAIT_TIMEOUT) -> bool:
    """Poll *predicate* (a subprocess-returning callable) until it succeeds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except (subprocess.SubprocessError, FileNotFoundError):
            pass
        time.sleep(POLL_INTERVAL)
    return False


def _postgres_ready(env: dict[str, str]) -> bool:
    result = _compose(
        "exec", "-T", "postgres",
        "pg_isready", "-U", POSTGRES_USER, "-d", POSTGRES_DB,
        env=env, check=False,
    )
    return result.returncode == 0


def _kafka_ready(env: dict[str, str]) -> bool:
    result = _compose(
        "exec", "-T", "kafka",
        "/opt/bitnami/kafka/bin/kafka-topics.sh",
        "--bootstrap-server", "localhost:9092", "--list",
        env=env, check=False,
    )
    return result.returncode == 0


def _consumer_running() -> bool:
    result = subprocess.run(
        [
            "docker", "inspect",
            "--format", "{{.State.Running}}",
            CONSUMER_CONTAINER,
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _run_migrations() -> None:
    """Apply the Alembic migrations to the Compose Postgres from the host.

    The gateway container normally performs startup auto-migration, but the
    consumer image ships without Alembic — so the smoke test applies the
    migrations itself (same driver the repo's integration tests use).
    """
    env = os.environ.copy()
    env.update(
        {
            "GATEWAY_ENV": "development",
            "GATEWAY_DATABASE_HOST": "localhost",
            "GATEWAY_DATABASE_PORT": "5432",
            "GATEWAY_DATABASE_NAME": POSTGRES_DB,
            "GATEWAY_DATABASE_USER": POSTGRES_USER,
            "GATEWAY_DATABASE_PASSWORD": POSTGRES_PASSWORD,
        }
    )
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _produce_normalized_event(env: dict[str, str]) -> None:
    """Produce one normalized event onto the configured topic via the broker."""
    _compose(
        "exec", "-T", "kafka",
        "/opt/bitnami/kafka/bin/kafka-console-producer.sh",
        "--bootstrap-server", "localhost:9092",
        "--topic", TOPIC,
        env=env,
        check=True,
        input=_normalized_event() + "\n",
    )


def _dsn() -> str:
    return (
        f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
        f"@localhost:5432/{POSTGRES_DB}"
    )


async def _event_ingested() -> bool:
    """True once the produced event exists in ``engineering_events``."""
    try:
        conn = await asyncio.wait_for(asyncpg.connect(dsn=_dsn(), timeout=5), timeout=10)
    except Exception:
        return False
    try:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM engineering_events "
            "WHERE event_type = 'issue.opened' AND external_id = $1 AND repository = $2",
            EXTERNAL_ID,
            REPOSITORY,
        )
        return int(count) >= 1
    finally:
        await conn.close()


@pytest.mark.integration
class TestAfkConsumerDeploymentSmoke:
    """End-to-end smoke test: the deployed consumer receives a normalized event."""

    @pytest.fixture(scope="class", autouse=True)
    def deployed_stack(self) -> dict[str, str]:
        """Bring up Postgres + Kafka + the AFK consumer, yield the compose env,
        and tear the three containers down afterwards."""
        if not _docker_compose_available():
            pytest.skip("docker compose and a running Docker daemon are required")

        env = os.environ.copy()
        # The consumer fails fast on an empty repository (Settings validator),
        # so the smoke test pins one.  Topic variables are pinned explicitly so
        # the test never produces to a caller-overridden topic.
        env.setdefault("GATEWAY_AFK_OUTCOMES_REPOSITORY", "octocat/Hello-World")
        env["GATEWAY_NORMALIZED_EVENTS_TOPIC"] = TOPIC
        env["GATEWAY_NORMALIZED_EVENTS_DLQ_TOPIC"] = f"{TOPIC}.dlq"

        _compose("up", "-d", "--build", *SERVICES, env=env)

        try:
            if not _wait_until(lambda: _postgres_ready(env)):
                raise TimeoutError("Postgres did not become ready in time")
            if not _wait_until(lambda: _kafka_ready(env)):
                raise TimeoutError("Kafka did not become ready in time")
            _run_migrations()
            if not _wait_until(_consumer_running):
                raise TimeoutError("AFK outcome consumer container did not start")
            yield env
        finally:
            # Stop + remove only the three containers this test started; the
            # shared named volume and any other running services are left alone.
            _compose("rm", "-sf", *SERVICES, env=env, check=False)

    @pytest.mark.asyncio
    async def test_consumer_receives_normalized_event(self, deployed_stack) -> None:
        """A normalized event produced on the configured topic is ingested into
        ``engineering_events`` by the deployed consumer."""
        _produce_normalized_event(deployed_stack)

        deadline = time.monotonic() + WAIT_TIMEOUT
        ingested = False
        while time.monotonic() < deadline:
            if await _event_ingested():
                ingested = True
                break
            await asyncio.sleep(POLL_INTERVAL)

        assert ingested, (
            f"The deployed consumer did not ingest the normalized event from "
            f"topic {TOPIC!r} within {WAIT_TIMEOUT}s.  Check the consumer's "
            f"topic configuration and container logs "
            f"(docker logs {CONSUMER_CONTAINER})."
        )
