"""Fail-closed guards for destructive disposable-PostgreSQL integration tests.

The AFK lifecycle concurrency and reporting ingestion suites migrate the target
database to ``head`` and, when the migration leaves a corrupt schema behind,
drop every table in the ``public`` schema before retrying.  They must only ever
run against an explicitly disposable PostgreSQL 16 instance — a local
Docker/Podman container or a GitHub Actions service container — never a
shared, production, or Kubernetes-managed database.

:func:`verify_disposable_target` positively verifies all of the following
before a caller executes any destructive statement:

* the operator explicitly attests that the target is disposable
  (``GATEWAY_TEST_DISPOSABLE_DB=1``);
* ``GATEWAY_ENV`` is not production;
* the target database name ends with ``_test``;
* the process is not running inside Kubernetes and the host is not an
  in-cluster service name;
* the server is PostgreSQL 16.

Any failed check raises :class:`DisposableDatabaseError`.  Callers must let
that exception propagate — pytest reports it as an error.  It must never be
translated into a skip: a skipped safety check is a failed safety check.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import asyncpg

#: Explicit operator attestation that the configured database is disposable.
DISPOSABLE_ATTESTATION_ENV = "GATEWAY_TEST_DISPOSABLE_DB"

#: The only PostgreSQL major version permitted for the isolated CI gate.
REQUIRED_POSTGRES_MAJOR = 16

_DISPOSABLE_DB_SUFFIX = "_test"
_FORBIDDEN_ENVIRONMENTS = frozenset({"production", "prod"})


class DisposableDatabaseError(RuntimeError):
    """The configured database cannot be proven to be a disposable target."""


@dataclass(frozen=True)
class DatabaseTarget:
    """Connection parameters for the disposable test database."""

    host: str
    port: int
    name: str
    user: str
    password: str

    @classmethod
    def from_env(cls) -> DatabaseTarget:
        """Resolve the target from the standard ``GATEWAY_DATABASE_*`` variables."""
        return cls(
            host=os.environ.get("GATEWAY_DATABASE_HOST", "localhost"),
            port=int(os.environ.get("GATEWAY_DATABASE_PORT", "5433")),
            name=os.environ.get("GATEWAY_DATABASE_NAME", "opencode_gateway_test"),
            user=os.environ.get("GATEWAY_DATABASE_USER", "opencode_test"),
            password=os.environ.get("GATEWAY_DATABASE_PASSWORD", "opencode_test"),
        )

    def dsn(self) -> str:
        return (
            f"postgresql://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.name}"
        )

    def describe(self) -> str:
        """Human-readable target for failure messages (never includes the password)."""
        return f"{self.host}:{self.port}/{self.name}"


def _require_environment_attestation(target: DatabaseTarget) -> None:
    if os.environ.get(DISPOSABLE_ATTESTATION_ENV) != "1":
        raise DisposableDatabaseError(
            f"Refusing destructive database operations against {target.describe()}: "
            f"{DISPOSABLE_ATTESTATION_ENV}=1 is required to attest that the target "
            "PostgreSQL database is disposable. These tests migrate the schema and "
            "can drop every table in the public schema."
        )

    environment = os.environ.get("GATEWAY_ENV", "production").strip().lower()
    if environment in _FORBIDDEN_ENVIRONMENTS:
        raise DisposableDatabaseError(
            f"Refusing destructive database operations against {target.describe()}: "
            f"GATEWAY_ENV={environment!r}. Set GATEWAY_ENV=development for the "
            "isolated disposable database."
        )


def _require_disposable_database_name(target: DatabaseTarget) -> None:
    if not target.name.lower().endswith(_DISPOSABLE_DB_SUFFIX):
        raise DisposableDatabaseError(
            f"Refusing destructive database operations against database "
            f"{target.name!r}: a disposable database name must end with "
            f"{_DISPOSABLE_DB_SUFFIX!r}."
        )


def _require_not_in_kubernetes(target: DatabaseTarget) -> None:
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        raise DisposableDatabaseError(
            "Refusing destructive database operations: this process is running "
            "inside Kubernetes (KUBERNETES_SERVICE_HOST is set). Run these tests "
            "against a local Docker/Podman container or a GitHub Actions service "
            "container instead."
        )

    host = target.host.strip().lower()
    if host == "kubernetes.default" or ".svc." in host or host.endswith(".svc"):
        raise DisposableDatabaseError(
            f"Refusing destructive database operations against Kubernetes service "
            f"host {target.host!r}. Use a local disposable container instead."
        )


async def _require_postgres_16(
    conn: asyncpg.Connection, target: DatabaseTarget
) -> None:
    server_version_num = int(await conn.fetchval("SHOW server_version_num"))
    major = server_version_num // 10000
    if major != REQUIRED_POSTGRES_MAJOR:
        raise DisposableDatabaseError(
            f"Refusing destructive database operations against {target.describe()}: "
            f"PostgreSQL {major} detected, but this gate requires PostgreSQL "
            f"{REQUIRED_POSTGRES_MAJOR}."
        )


async def verify_disposable_target(
    target: DatabaseTarget | None = None,
) -> DatabaseTarget:
    """Verify the target is an explicitly disposable PostgreSQL 16 database.

    Runs every environment, naming, and Kubernetes check before connecting so
    an unverifiable target fails closed without touching the server.  Once
    connected, checks the actual database identity and server major version.
    Raises :class:`DisposableDatabaseError` on any failure, including an
    unreachable database — never returns a skip signal.
    """
    resolved = target or DatabaseTarget.from_env()

    _require_environment_attestation(resolved)
    _require_disposable_database_name(resolved)
    _require_not_in_kubernetes(resolved)

    try:
        conn = await asyncpg.connect(dsn=resolved.dsn(), timeout=5)
    except Exception as exc:
        raise DisposableDatabaseError(
            f"Required disposable PostgreSQL database {resolved.describe()} is "
            f"unreachable: {exc}. Start one outside Kubernetes, for example:\n"
            "  docker run --rm -d --name opencode-gateway-test-db \\\n"
            "    -e POSTGRES_DB=opencode_gateway_test \\\n"
            "    -e POSTGRES_USER=opencode_test \\\n"
            "    -e POSTGRES_PASSWORD=opencode_test \\\n"
            "    -p 5433:5432 postgres:16-alpine"
        ) from exc

    try:
        current_database = await conn.fetchval("SELECT current_database()")
        if current_database != resolved.name:
            raise DisposableDatabaseError(
                f"Refusing destructive database operations: connected to "
                f"{current_database!r} but expected {resolved.name!r}."
            )
        await _require_postgres_16(conn, resolved)
    except DisposableDatabaseError:
        raise
    except Exception as exc:
        raise DisposableDatabaseError(
            f"Could not verify disposable PostgreSQL target "
            f"{resolved.describe()}: {exc}"
        ) from exc
    finally:
        await conn.close()

    return resolved
