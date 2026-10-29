"""Fail-closed preconditions for destructive and required integration tests.

The AFK lifecycle concurrency, reporting ingestion, AFK consumer, AFK backfill
CLI, reporting aggregate, and production-scale overlap suites migrate the
schema to ``head`` and may drop every table in the ``public`` schema.  They
must only ever run against an explicitly disposable PostgreSQL 16 instance —
a local Docker/Podman container or the dedicated GitHub Actions
``postgres-isolated`` service container — never a shared, production, or
Kubernetes-managed database.

:func:`verify_disposable_target` positively verifies all of the following
before a caller executes any destructive statement:

* the operator explicitly attests that the target is disposable
  (``GATEWAY_TEST_DISPOSABLE_DB=1``);
* ``GATEWAY_ENV`` is not production;
* the target is the dedicated disposable service profile: loopback host,
  port 5433, an approved database name — ``opencode_gateway_disposable_test``
  for the required gates or ``opencode_gateway_benchmark_test`` for the
  optional benchmark workflow — and the dedicated ``test`` user with a
  non-empty password;
* the target database name ends with ``_test``;
* the process is not running inside Kubernetes and the host is not an
  in-cluster service name;
* the server is PostgreSQL 16.

Any failed check raises :class:`DisposableDatabaseError`.  Callers must let
that exception propagate — pytest reports it as an error.  It must never be
translated into a skip: a skipped safety check is a failed safety check.

:func:`verify_required_test_report` is the companion non-skip contract for
required CI jobs.  A JUnit report that is missing, empty, contains any
skipped/failed/errored case, collects fewer tests than required, or does not
match an expected test-name set raises :class:`RequiredTestExecutionError`.
An all-skipped run is therefore a failure, never a silent green.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import asyncpg

#: Explicit operator attestation that the configured database is disposable.
DISPOSABLE_ATTESTATION_ENV = "GATEWAY_TEST_DISPOSABLE_DB"

#: The only PostgreSQL major version permitted for the isolated CI gate.
REQUIRED_POSTGRES_MAJOR = 16

#: The dedicated CI ``postgres-isolated`` service profile.  Destructive suites
#: are only ever allowed to target this exact loopback service; anything else
#: (including the shared ``postgres`` service on 5432) is refused before the
#: first connection attempt.
DISPOSABLE_DATABASE_NAME = "opencode_gateway_disposable_test"

#: The separate isolated database provisioned by the optional overlap
#: benchmark workflow on the same loopback/``test``-user service profile.
BENCHMARK_DATABASE_NAME = "opencode_gateway_benchmark_test"

DISPOSABLE_DATABASE_USER = "test"
DISPOSABLE_DATABASE_PORT = 5433

#: Every database name the dedicated service profile may target.  Both names
#: are explicit ``_test`` databases; nothing else is ever accepted.
APPROVED_DATABASE_NAMES = frozenset({DISPOSABLE_DATABASE_NAME, BENCHMARK_DATABASE_NAME})

_DISPOSABLE_DB_SUFFIX = "_test"
_APPROVED_HOSTS = frozenset({"localhost", "127.0.0.1"})
_FORBIDDEN_ENVIRONMENTS = frozenset({"production", "prod"})


class DisposableDatabaseError(RuntimeError):
    """The configured database cannot be proven to be a disposable target."""


class RequiredTestExecutionError(RuntimeError):
    """A required test run did not prove that the expected tests executed."""


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
        """Resolve the target from the standard ``GATEWAY_DATABASE_*`` variables.

        Defaults describe the dedicated disposable service so a developer who
        follows the documented ``docker run`` command needs no overrides.
        """
        return cls(
            host=os.environ.get("GATEWAY_DATABASE_HOST", "localhost"),
            port=int(
                os.environ.get("GATEWAY_DATABASE_PORT", str(DISPOSABLE_DATABASE_PORT))
            ),
            name=os.environ.get("GATEWAY_DATABASE_NAME", DISPOSABLE_DATABASE_NAME),
            user=os.environ.get("GATEWAY_DATABASE_USER", DISPOSABLE_DATABASE_USER),
            password=os.environ.get("GATEWAY_DATABASE_PASSWORD", "test"),
        )

    def dsn(self) -> str:
        return (
            f"postgresql://{self.user}:{self.password}"
            f"@{self.host}:{self.port}/{self.name}"
        )

    def describe(self) -> str:
        """Human-readable target for failure messages (never includes the password)."""
        return f"{self.host}:{self.port}/{self.name}"


@dataclass(frozen=True)
class JUnitExecutionSummary:
    """Counts parsed from a pytest JUnit report."""

    total: int
    passed: int
    skipped: int
    failed: int
    errors: int


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


def _require_dedicated_ci_profile(target: DatabaseTarget) -> None:
    """Only the dedicated ``postgres-isolated`` service may be targeted."""
    host = target.host.strip().lower()
    if host not in _APPROVED_HOSTS:
        raise DisposableDatabaseError(
            f"Refusing destructive database operations against {target.describe()}: "
            f"host {target.host!r} is not an approved loopback host. Destructive "
            f"suites may only target localhost:{DISPOSABLE_DATABASE_PORT}."
        )

    if target.port != DISPOSABLE_DATABASE_PORT:
        raise DisposableDatabaseError(
            f"Refusing destructive database operations against {target.describe()}: "
            f"port {target.port} is not the approved disposable service port "
            f"{DISPOSABLE_DATABASE_PORT}."
        )

    if target.name not in APPROVED_DATABASE_NAMES:
        raise DisposableDatabaseError(
            f"Refusing destructive database operations against database "
            f"{target.name!r}: destructive suites may only target the approved "
            "disposable databases "
            f"{sorted(APPROVED_DATABASE_NAMES)!r}."
        )

    if target.user != DISPOSABLE_DATABASE_USER:
        raise DisposableDatabaseError(
            f"Refusing destructive database operations against {target.describe()}: "
            f"user {target.user!r} is not the dedicated disposable-service user "
            f"{DISPOSABLE_DATABASE_USER!r}."
        )

    if not target.password:
        raise DisposableDatabaseError(
            f"Refusing destructive database operations against {target.describe()}: "
            "the dedicated disposable-service password is empty."
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

    Runs every environment, allowlist, naming, and Kubernetes check before
    connecting so an unverifiable target fails closed without touching the
    server.  Once connected, checks the actual database identity and server
    major version using read-only statements only.
    Raises :class:`DisposableDatabaseError` on any failure, including an
    unreachable database — never returns a skip signal.
    """
    resolved = target or DatabaseTarget.from_env()

    _require_environment_attestation(resolved)
    _require_dedicated_ci_profile(resolved)
    _require_disposable_database_name(resolved)
    _require_not_in_kubernetes(resolved)

    try:
        conn = await asyncpg.connect(dsn=resolved.dsn(), timeout=5)
    except Exception as exc:
        raise DisposableDatabaseError(
            f"Required disposable PostgreSQL database {resolved.describe()} is "
            f"unreachable: {exc}. Start one outside Kubernetes, for example:\n"
            "  docker run --rm -d --name opencode-gateway-test-db \\\n"
            f"    -e POSTGRES_DB={DISPOSABLE_DATABASE_NAME} \\\n"
            f"    -e POSTGRES_USER={DISPOSABLE_DATABASE_USER} \\\n"
            f"    -e POSTGRES_PASSWORD={DISPOSABLE_DATABASE_USER} \\\n"
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


def verify_required_test_report(
    report_path: str | os.PathLike[str],
    *,
    expected_names: Iterable[str] | None = None,
    minimum_executed: int = 1,
) -> JUnitExecutionSummary:
    """Fail closed unless a pytest JUnit report proves required tests ran.

    ``expected_names`` may pin the exact set of test-case names a required job
    must collect (useful for the six-case isolated gate and small suites).
    ``minimum_executed`` is the minimum number of passing cases for larger
    suites where naming every case would be brittle.

    Raises :class:`RequiredTestExecutionError` when the report is missing or
    malformed, collects zero cases, contains any skipped/failed/errored case,
    collects fewer passing cases than ``minimum_executed``, or does not match
    ``expected_names``.  An all-skipped run can never pass this check.
    """
    path = Path(report_path)
    if not path.exists():
        raise RequiredTestExecutionError(
            f"Required JUnit report is missing: {path}. A required test step "
            "must produce a report; a missing report is a failure, not a skip."
        )

    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise RequiredTestExecutionError(
            f"Required JUnit report {path} could not be parsed: {exc}"
        ) from exc

    cases = list(root.iter("testcase"))
    problems: list[str] = []
    names: list[str] = []
    passed = 0
    skipped = 0
    failed = 0
    errors = 0

    for case in cases:
        name = case.get("name", "<unnamed>")
        classname = case.get("classname", "<unknown>")
        names.append(name)
        if case.find("skipped") is not None:
            skipped += 1
            problems.append(f"{classname}::{name} was skipped")
        elif case.find("failure") is not None:
            failed += 1
            problems.append(f"{classname}::{name} failed")
        elif case.find("error") is not None:
            errors += 1
            problems.append(f"{classname}::{name} errored")
        else:
            passed += 1

    if not cases:
        problems.append("the report contains no test cases")

    if expected_names is not None:
        expected = set(expected_names)
        missing = sorted(expected - set(names))
        unexpected = sorted(set(names) - expected)
        if len(cases) != len(expected):
            problems.append(
                f"expected {len(expected)} test cases, collected {len(cases)}"
            )
        if missing:
            problems.append(f"missing test cases: {missing}")
        if unexpected:
            problems.append(f"unexpected test cases: {unexpected}")

    if passed < minimum_executed:
        problems.append(
            f"expected at least {minimum_executed} passing test cases, "
            f"observed {passed}"
        )

    if problems:
        detail = "\n".join(f"  - {problem}" for problem in problems)
        raise RequiredTestExecutionError(
            f"Required test execution contract failed for {path}:\n{detail}"
        )

    return JUnitExecutionSummary(
        total=len(cases),
        passed=passed,
        skipped=skipped,
        failed=failed,
        errors=errors,
    )
