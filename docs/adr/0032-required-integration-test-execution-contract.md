# 0032 — Required integration tests use a disposable-Postgres attestation and a non-skip execution contract

Status: accepted

## Context

Parent issue #801 audited the integration suite and found five modules that are
not exercised by a required CI job:

| Suite | Current skip condition | Destructive setup | Test-database assumption | Classification |
| --- | --- | --- | --- | --- |
| `tests/integration/test_afk_consumer.py` | `pytest.skip` when the database is unreachable | Alembic upgrade to `head`, drop-all retry, drop-all teardown | Dedicated disposable PostgreSQL 16 at loopback:5433 | Required once enrolled (#833); disposable-only destructive setup |
| `tests/integration/test_afk_outcomes_cli.py` | `pytest.skip` when the database is unreachable | Alembic upgrade to `head`, drop-all retry, drop-all teardown | Dedicated disposable PostgreSQL 16 at loopback:5433 | Required once enrolled (#833); disposable-only destructive setup |
| `tests/integration/test_reporting_aggregates.py` | `pytest.skip` when the database is unreachable | Alembic upgrade to `head`, drop-all retry, drop-all teardown | Dedicated disposable PostgreSQL 16 at loopback:5433 | Required once enrolled (#834); disposable-only destructive setup |
| `tests/integration/test_ingest_batch_overlap_perf.py` | `pytest.skip` when the database is unreachable | Alembic upgrade to `head`, ~123k-row fixture, drop-all teardown | Dedicated disposable PostgreSQL 16 at loopback:5433, sized for the large fixture | Optional benchmark (#835); disposable-only when invoked |
| `tests/integration/test_read_path_live_protocol.py` | `pytest.skip` when no live database is configured | None — read-only transaction, `SELECT`/`EXPLAIN` only | Live read-only replica via `READ_PATH_LIVE_DATABASE_URL` or `GATEWAY_DATABASE_*` | Read-only opt-in protocol (#836); not subject to the disposable attestation |

A skipped safety check is a failed safety check.  Before any of these suites is
enrolled, two preconditions must be enforceable:

1. destructive setup/teardown must only target the dedicated disposable
   PostgreSQL service, never a shared, production, or Kubernetes-managed
   database;
2. a required CI job must fail, not go green, when the database is absent or
   every test is skipped.

The existing six-case isolated gate from issue #784
(`test_afk_lifecycle_concurrency.py` + `test_reporting_ingest.py` with JUnit
verification) must remain green and must not be weakened.

## Decision

### Dedicated disposable target

Destructive suites call
`tests.integration._disposable_postgres.verify_disposable_target` before any
migration or write.  The guard accepts exactly one service profile with one of
two approved database names:

* `GATEWAY_TEST_DISPOSABLE_DB=1` operator attestation;
* `GATEWAY_ENV` is not `production`/`prod`;
* loopback host (`localhost` or `127.0.0.1`);
* port `5433` — the `postgres-isolated` GitHub Actions service mapping;
* database `opencode_gateway_disposable_test` for the required gates, or
  `opencode_gateway_benchmark_test` for the optional benchmark workflow — both
  are the separate isolated instances on the same `loopback:5433`/`test`
  profile (see `.github/workflows/benchmark-overlap.yml`);
* user `test` with a non-empty password;
* database name ends with `_test`;
* not running inside Kubernetes and not an in-cluster service host;
* reachable PostgreSQL 16.

Every failure raises `DisposableDatabaseError`.  Callers must never translate
that error into a skip.  The guard performs only read-only probes
(`SELECT current_database()`, `SHOW server_version_num`) after the allowlist
checks pass; a mismatched or unapproved DSN is rejected before the first
connection attempt.

The dedicated profile supersedes the older `docker-compose.test.yml` defaults
(`opencode_gateway_test` / `opencode_test`, PostgreSQL 15): local runs must use
the documented PostgreSQL 16 `docker run` container with the dedicated names.

### Required-mode non-skip contract

A required job verifies its pytest JUnit report with
`verify_required_test_report`.  The check fails when the report is missing or
malformed, collects zero cases, contains any skipped/failed/errored case,
collects fewer passing cases than `minimum_executed`, or does not match an
optional exact `expected_names` set.  An all-skipped run therefore fails.

Small suites may pin `expected_names` (the six-case gate does).  Larger suites
may use `minimum_executed` instead of naming every case.

### Preserved gate

The six-case isolated gate from #784 keeps its existing JUnit verifier and its
service/database mapping.  The stricter guard is additive: the same CI
environment already satisfies it.

### Classification and reuse

* **Required** (once enrolled by #833/#834): AFK consumer, AFK backfill CLI,
  reporting aggregates.  Destructive setup must use the disposable guard.
* **Optional benchmark** (#835): production-scale overlap suite.  It must not
  run in required pull-request CI, but when invoked it uses the same guard and
  fails closed.
* **Read-only protocol** (#836): live read-path measurement.  It performs no
  writes, keeps its read-only transaction, and remains explicitly opt-in; it
  must not be conflated with the disposable-database attestation.

Subsequent suite-enablement children reuse the guard and the JUnit verifier
rather than re-implementing either.

## Consequences

* A destructive suite can no longer run by accident against a shared or
  production database; the failure is explicit and occurs before writes.
* A missing database or all-skipped run becomes a red required check.
* Local destructive runs require the dedicated container names and
  `GATEWAY_TEST_DISPOSABLE_DB=1`; the old Compose defaults are no longer
  accepted.
* Required CI gains a small self-check that exercises the guard and JUnit
  contract without connecting to a database.
