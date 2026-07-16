"""Tests for secret redaction helpers (app.core.secrets) and the
RedactingFormatter (app.core.logging).

Covers the acceptance criteria for issue #105:
1. Secret-like env var values are never logged/evented in plaintext
2. Executor logs redact
3. Tests cover common secret key names and nested metadata redaction
"""

from __future__ import annotations

import logging

import pytest

from app.core.logging import RedactingFormatter
from app.core.secrets import REDACTED, is_secret_key, redact_dict

# ============================================================================
# is_secret_key
# ============================================================================


class TestIsSecretKey:
    """Tests for :func:`is_secret_key` — key-name detection."""

    # ── keys that SHOULD be detected as secrets ─────────────────────────

    @pytest.mark.parametrize(
        "key",
        [
            "TOKEN",
            "token",
            "GITHUB_TOKEN",
            "github_token",
            "GITLAB_TOKEN",
            "NPM_TOKEN",
            "PASSWORD",
            "password",
            "DATABASE_PASSWORD",
            "db_password",
            "POSTGRES_PASSWORD",
            "SECRET",
            "secret",
            "MY_SECRET",
            "AWS_SECRET_KEY",
            "SECRET_KEY",
            "API_KEY",
            "api_key",
            "OPENAI_API_KEY",
            "CREDENTIAL",
            "CREDENTIALS",
            "AWS_CREDENTIALS",
            "AUTH",
            "AUTHORIZATION",
            "BEARER_TOKEN",
            "ACCESS_TOKEN",
            "REFRESH_TOKEN",
            "PRIVATE_KEY",
            "SSH_PRIVATE_KEY",
            "GCP_CREDENTIALS_JSON",
            "AZURE_CLIENT_SECRET",
            "SENDGRID_API_KEY",
            "STRIPE_SECRET_KEY",
            "WEBHOOK_SECRET",
        ],
    )
    def test_secret_keys_detected(self, key: str):
        """Known secret-like key names should return True."""
        assert is_secret_key(key), f"Expected {key!r} to be detected as secret"

    # ── keys that should NOT be detected as secrets ─────────────────────

    @pytest.mark.parametrize(
        "key",
        [
            "REPO_URL",
            "repo_url",
            "BRANCH",
            "branch",
            "WORKSPACE_PATH",
            "PORT",
            "HOST",
            "USER",
            "LOG_LEVEL",
            "TIMEOUT",
            "INTERVAL",
            "RETENTION_HOURS",
            "THRESHOLD_PERCENT",
            "BATCH_SIZE",
            "EXECUTOR_TYPE",
            "DATABASE_HOST",
            "DATABASE_PORT",
            "DATABASE_NAME",
            "DATABASE_USER",
            "MY_VAR",
            "FOO",
            "BAR",
            "NODE_ENV",
            "PYTHONPATH",
            "DEBUG",
        ],
    )
    def test_non_secret_keys_passed_through(self, key: str):
        """Non-secret key names should return False."""
        assert not is_secret_key(key), f"Expected {key!r} to NOT be detected as secret"

    # ── edge cases ──────────────────────────────────────────────────────

    def test_empty_string(self):
        """Empty string is not a secret key."""
        assert not is_secret_key("")

    def test_underscore_only(self):
        """A key that is only underscores should not match."""
        assert not is_secret_key("_")

    def test_partial_match_within_word(self):
        """Substring matching is greedy: 'AUTHOR' contains 'auth' so it is
        detected.  This is an accepted trade-off for simplicity — env var
        names like ``AUTHOR`` are extremely rare and would not contain
        real secrets."""
        assert is_secret_key("AUTHOR")  # contains 'auth'
        assert is_secret_key("AUTHORIZATION")  # contains 'auth'


# ============================================================================
# redact_dict
# ============================================================================


class TestRedactDict:
    """Tests for :func:`redact_dict` — the core redaction function."""

    # ── basic redaction ─────────────────────────────────────────────────

    def test_redacts_single_secret(self):
        result = redact_dict({"GITHUB_TOKEN": "ghp_abc123"})
        assert result == {"GITHUB_TOKEN": REDACTED}

    def test_redacts_multiple_secrets(self):
        result = redact_dict({
            "DATABASE_PASSWORD": "s3cret",
            "API_KEY": "sk-abc",
            "REPO_URL": "https://example.com/repo.git",
        })
        assert result == {
            "DATABASE_PASSWORD": REDACTED,
            "API_KEY": REDACTED,
            "REPO_URL": "https://example.com/repo.git",
        }

    def test_preserves_non_secret_values(self):
        data = {
            "REPO_URL": "https://github.com/org/repo",
            "BRANCH": "main",
            "LOG_LEVEL": "debug",
            "WORKSPACE_PATH": "/home/runner/ws/123",
            "PORT": "8080",
        }
        result = redact_dict(data)
        assert result == data

    # ── nested dicts ────────────────────────────────────────────────────

    def test_redacts_nested_secrets(self):
        result = redact_dict({
            "config": {
                "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
                "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                "region": "us-east-1",
            }
        })
        assert result == {
            "config": {
                "AWS_ACCESS_KEY_ID": REDACTED,
                "AWS_SECRET_ACCESS_KEY": REDACTED,
                "region": "us-east-1",
            }
        }

    def test_deeply_nested_secrets(self):
        result = redact_dict({
            "a": {
                "b": {
                    "c": {
                        "PASSWORD": "top-secret",
                        "name": "test",
                    }
                }
            }
        })
        assert result == {
            "a": {
                "b": {
                    "c": {
                        "PASSWORD": REDACTED,
                        "name": "test",
                    }
                }
            }
        }

    def test_mixed_nesting(self):
        """Top-level non-secret with nested secrets."""
        result = redact_dict({
            "action": "start",
            "workspace_path": "/home/runner/ws/1",
            "env": {
                "DATABASE_URL": "postgres://user:pass@host/db",
                "SECRET_KEY": "django-insecure-abc",
            },
        })
        assert result == {
            "action": "start",
            "workspace_path": "/home/runner/ws/1",
            "env": {
                "DATABASE_URL": "postgres://user:pass@host/db",
                "SECRET_KEY": REDACTED,
            },
        }

    # ── custom placeholder ───────────────────────────────────────────────

    def test_custom_placeholder(self):
        result = redact_dict({"TOKEN": "abc"}, placeholder="[REDACTED]")
        assert result == {"TOKEN": "[REDACTED]"}

    # ── edge cases ──────────────────────────────────────────────────────

    def test_empty_dict(self):
        assert redact_dict({}) == {}

    def test_no_secrets(self):
        data = {"foo": "bar", "baz": "qux"}
        assert redact_dict(data) == data

    def test_none_values_preserved(self):
        result = redact_dict({"TOKEN": None, "REPO_URL": "x"})
        assert result == {"TOKEN": REDACTED, "REPO_URL": "x"}

    def test_int_values_preserved_for_non_secrets(self):
        result = redact_dict({"PORT": 8080, "TIMEOUT": 300})
        assert result == {"PORT": 8080, "TIMEOUT": 300}

    def test_does_not_modify_original_dict(self):
        original = {"TOKEN": "secret123", "repo": "my-repo"}
        redact_dict(original)
        assert original == {"TOKEN": "secret123", "repo": "my-repo"}

    def test_empty_nested_dict(self):
        result = redact_dict({"outer": {}})
        assert result == {"outer": {}}

    # ── regression: compound key names ───────────────────────────────────

    def test_compound_key_names(self):
        """Keys with underscores separating secret-like fragments should match."""
        result = redact_dict({
            "AWS_ACCESS_KEY_ID": "AKIA123",
            "GOOGLE_APPLICATION_CREDENTIALS": "/path/to/creds.json",
            "REPO_URL": "safe-value",
        })
        assert result == {
            "AWS_ACCESS_KEY_ID": REDACTED,
            "GOOGLE_APPLICATION_CREDENTIALS": REDACTED,
            "REPO_URL": "safe-value",
        }


# ============================================================================
# RedactingFormatter
# ============================================================================


class TestRedactingFormatter:
    """Tests for the :class:`RedactingFormatter` — belt-and-suspenders
    protection for log messages that might still contain secrets.

    The regex-based formatter handles these formats:
    * ``KEY=VALUE`` (unquoted value)
    * ``KEY="VALUE"`` (double-quoted value)
    * ``KEY='VALUE'`` (single-quoted value)
    * ``KEY: VALUE`` (colon-separated)

    It does NOT handle repr-style ``{'KEY': 'VALUE'}`` patterns because
    the primary defence is :func:`redact_dict` applied before logging.
    """

    @staticmethod
    def _format_message(msg: str, *args: object) -> str:
        """Helper: format a log record through RedactingFormatter."""
        formatter = RedactingFormatter(fmt="%(message)s")
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=0,
            msg=msg,
            args=args,
            exc_info=None,
        )
        return formatter.format(record)

    # ── KEY=VALUE patterns ──────────────────────────────────────────────

    def test_equal_sign_format(self):
        result = self._format_message(
            "start_opencode: workspace=ws1 GITHUB_TOKEN=ghp_abc123 branch=main"
        )
        assert "ghp_abc123" not in result
        assert "GITHUB_TOKEN=***" in result
        assert "workspace=ws1" in result
        assert "branch=main" in result

    def test_quoted_value_format(self):
        """Double-quoted values should be redacted."""
        result = self._format_message(
            'config DATABASE_PASSWORD="s3cret-value" host=localhost'
        )
        assert "s3cret-value" not in result
        assert 'DATABASE_PASSWORD="***"' in result
        assert "host=localhost" in result

    def test_colon_format(self):
        result = self._format_message(
            "config DATABASE_PASSWORD: s3cret, HOST: localhost"
        )
        assert "s3cret" not in result
        assert "DATABASE_PASSWORD: ***" in result
        assert "HOST: localhost" in result

    def test_multiple_secrets_in_one_message(self):
        result = self._format_message(
            "API_KEY=sk-abc SECRET_KEY=django-secret BRANCH=main"
        )
        assert "sk-abc" not in result
        assert "django-secret" not in result
        assert "API_KEY=***" in result
        assert "SECRET_KEY=***" in result
        assert "BRANCH=main" in result

    # ── no false positives ──────────────────────────────────────────────

    def test_non_secret_key_not_redacted(self):
        result = self._format_message(
            "create_workspace repo=https://example.com branch=feature/x"
        )
        assert "https://example.com" in result
        assert "branch=feature/x" in result

    def test_single_word_values_not_redacted(self):
        result = self._format_message("status=running port=8080")
        assert "status=running" in result
        assert "port=8080" in result

    # ── edge cases ──────────────────────────────────────────────────────

    def test_message_with_no_secrets_unchanged(self):
        msg = "health check passed database=connected version=0.1.0"
        assert self._format_message(msg) == msg

    def test_empty_message(self):
        assert self._format_message("") == ""


# ============================================================================
# configure_root_logger smoke test
# ============================================================================


class TestConfigureRootLogger:
    """Smoke tests for :func:`configure_root_logger`."""

    def test_installs_redacting_handler(self):
        from app.core.logging import configure_root_logger

        configure_root_logger(level=logging.WARNING)

        root = logging.getLogger()
        handlers = root.handlers
        assert len(handlers) >= 1
        assert isinstance(handlers[0].formatter, RedactingFormatter)
        assert root.level == logging.WARNING
