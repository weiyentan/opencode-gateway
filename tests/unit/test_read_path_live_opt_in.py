"""Opt-in semantics for the live read-path measurement harness (issue #836).

The live protocol in ``tests/integration/test_read_path_live_protocol.py``
is deliberately opt-in: ordinary CI runs and local test runs must never
attempt a live measurement.  These tests exercise the harness's guards
without opening a database connection, proving that:

* nothing connects unless ``READ_PATH_LIVE_ENABLED`` was explicitly set;
* an explicit request with missing credentials fails (never skips green);
* an explicit request with an unreachable target fails and never leaks the
  configured DSN or its password;
* an explicit request that executes zero probes fails instead of reporting
  green measurement evidence.

The pure plan/latency helper tests remain in the integration module
(``TestPlanHelpers``) and are unaffected.
"""

from __future__ import annotations

from urllib.parse import unquote, urlsplit

import pytest

from tests.integration import test_read_path_live_protocol as harness


def _clear_live_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(harness.LIVE_ENABLED_ENV, raising=False)
    monkeypatch.delenv(harness.LIVE_DATABASE_URL_ENV, raising=False)
    for name in harness.GATEWAY_DB_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_not_requested_skips_safely_even_with_a_dsn(monkeypatch: pytest.MonkeyPatch):
    """A DSN alone is not opt-in; the fixture must skip without connecting."""
    _clear_live_env(monkeypatch)
    monkeypatch.setenv(
        harness.LIVE_DATABASE_URL_ENV,
        "postgresql://reader:secret@replica.internal:5432/gateway",
    )
    assert harness.live_opt_in_requested() is False

    with pytest.raises(pytest.skip.Exception, match="opt-in"):
        harness.live_database_dsn.__wrapped__()


def test_requested_without_credentials_fails_not_skips(monkeypatch: pytest.MonkeyPatch):
    _clear_live_env(monkeypatch)
    monkeypatch.setenv(harness.LIVE_ENABLED_ENV, "1")

    with pytest.raises(pytest.fail.Exception, match="credentials"):
        harness.live_database_dsn.__wrapped__()


def test_partial_gateway_credentials_fail_and_name_missing_fields(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_live_env(monkeypatch)
    monkeypatch.setenv(harness.LIVE_ENABLED_ENV, "1")
    monkeypatch.setenv("GATEWAY_DATABASE_HOST", "replica.internal")
    monkeypatch.setenv("GATEWAY_DATABASE_NAME", "gateway")
    monkeypatch.setenv("GATEWAY_DATABASE_USER", "reader")
    # Password deliberately absent.

    with pytest.raises(harness.LiveMeasurementUnavailable) as excinfo:
        harness.resolve_live_dsn()

    assert "GATEWAY_DATABASE_PASSWORD" in str(excinfo.value)


def test_requested_with_unreachable_target_fails_without_leaking_dsn(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_live_env(monkeypatch)
    monkeypatch.setenv(harness.LIVE_ENABLED_ENV, "1")
    monkeypatch.setenv(
        harness.LIVE_DATABASE_URL_ENV,
        "postgresql://reader:s3cret-should-not-leak@replica.invalid:5432/gateway",
    )

    async def unreachable(_dsn: str) -> bool:
        return False

    monkeypatch.setattr(harness, "_can_connect", unreachable)

    with pytest.raises(pytest.fail.Exception) as excinfo:
        harness.live_database_dsn.__wrapped__()

    message = str(excinfo.value)
    assert "explicitly requested" in message
    assert "replica.invalid:5432/gateway" in message
    assert "s3cret-should-not-leak" not in message
    assert "reader" not in message


def test_dsn_selection_prefers_explicit_live_url(monkeypatch: pytest.MonkeyPatch):
    _clear_live_env(monkeypatch)
    monkeypatch.setenv(harness.LIVE_ENABLED_ENV, "true")
    monkeypatch.setenv(
        harness.LIVE_DATABASE_URL_ENV,
        "postgresql://reader:secret@replica.internal:5432/gateway",
    )
    monkeypatch.setenv("GATEWAY_DATABASE_HOST", "ignored.internal")

    assert (
        harness.resolve_live_dsn()
        == "postgresql://reader:secret@replica.internal:5432/gateway"
    )


def test_dsn_selection_falls_back_to_complete_gateway_credentials(
    monkeypatch: pytest.MonkeyPatch,
):
    _clear_live_env(monkeypatch)
    monkeypatch.setenv(harness.LIVE_ENABLED_ENV, "1")
    monkeypatch.setenv("GATEWAY_DATABASE_HOST", "replica.internal")
    monkeypatch.setenv("GATEWAY_DATABASE_PORT", "5432")
    monkeypatch.setenv("GATEWAY_DATABASE_NAME", "gateway")
    monkeypatch.setenv("GATEWAY_DATABASE_USER", "reader")
    monkeypatch.setenv("GATEWAY_DATABASE_PASSWORD", "p@ss/word")

    dsn = harness.resolve_live_dsn()
    parsed = urlsplit(dsn)
    assert parsed.hostname == "replica.internal"
    assert parsed.port == 5432
    assert parsed.path == "/gateway"
    assert unquote(parsed.username or "") == "reader"
    assert unquote(parsed.password or "") == "p@ss/word"


def test_describe_target_strips_credentials():
    target = harness.describe_target(
        "postgresql://reader:s3cret@replica.internal:5432/gateway"
    )
    assert target == "replica.internal:5432/gateway"
    assert "s3cret" not in target
    assert "reader" not in target


def test_zero_executed_probes_cannot_report_green_evidence():
    with pytest.raises(harness.LiveMeasurementUnavailable, match="zero live probes"):
        harness.require_executed_probes([])

    assert harness.require_executed_probes(["aggregates_total"]) == ["aggregates_total"]


def test_probe_ledger_teardown_fails_when_no_probe_ran():
    generator = harness.live_probe_ledger.__wrapped__(
        "postgresql://reader@replica.internal:5432/gateway"
    )
    ledger = next(generator)
    assert ledger == []

    with pytest.raises(harness.LiveMeasurementUnavailable, match="zero live probes"):
        next(generator)
