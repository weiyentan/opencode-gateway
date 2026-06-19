"""Logging configuration with secret redaction.

Provides a :class:`RedactingFormatter` that intercepts formatted log
records and replaces plaintext secret values with ``***``.  The
formatter works on the final rendered string using a belt-and-suspenders
approach: the primary defence is that callers should pass pre-redacted
data to log statements (via :func:`app.core.secrets.redact_dict`), and
the formatter catches any remaining leaks from f-strings, exc_info,
or other dynamic message sources.

Usage::

    from app.core.logging import configure_root_logger
    configure_root_logger()
"""

from __future__ import annotations

import logging
import re

from app.core.secrets import REDACTED, is_secret_key

# ---------------------------------------------------------------------------
# Patterns that the formatter scans for in the final rendered message
# ---------------------------------------------------------------------------

# Matches ``KEY=value``, ``KEY: value``, ``KEY: "value"``, ``KEY='value'``,
# ``'KEY': 'value'``, ``"KEY": "value"``, and bare-token patterns.
#
# The pattern is deliberately conservative: it only matches keys that
# :func:`is_secret_key` would recognise, plus a few common token formats
# (Bearer tokens, GitHub tokens, hex secrets) that might appear directly
# in error messages.

_TOKEN_LIKE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Bearer / GitHub / generic token patterns — values that look like
    # tokens regardless of the key name.
    (re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*"), "Bearer ***"),
    (re.compile(r"\bgh[ps]_[A-Za-z0-9]{36,}\b"), "ghp_***"),
    (re.compile(r"\bgho_[A-Za-z0-9]{36,}\b"), "gho_***"),
    (re.compile(r"\bghu_[A-Za-z0-9]{36,}\b"), "ghu_***"),
    (re.compile(r"\bghs_[A-Za-z0-9]{36,}\b"), "ghs_***"),
    (re.compile(r"\bghr_[A-Za-z0-9]{36,}\b"), "ghr_***"),
]


class RedactingFormatter(logging.Formatter):
    """A :class:`logging.Formatter` that redacts secrets from log messages.

    After the standard formatting pass, the resulting string is scanned
    for:

    * ``KEY=VALUE`` patterns where *KEY* matches :func:`is_secret_key`.
    * Common token formats (Bearer tokens, GitHub PATs).

    Any matched value is replaced with ``"***""``.

    This is a secondary safeguard — the primary expectation is that
    callers pass pre-redacted data to logger calls.
    """

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        style: str = "%",
        *,
        redacted_placeholder: str = REDACTED,
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt, style=style)
        self._placeholder = redacted_placeholder
        # Pattern that matches any secret-like key in a key=value or
        # key: value context within the rendered log message.
        # Covers: KEY=VALUE, KEY="VALUE", KEY='VALUE', 'KEY': 'VALUE',
        # "KEY": "VALUE", and KEY='VALUE' in repr output.
        self._kv_pattern = re.compile(
            r"""(?ix)                                     # case-insensitive, verbose
            \b                                           # word boundary
            (?P<key>[A-Za-z_][A-Za-z0-9_]*)               # the key name
            \s*[:=]\s*                                    # separator : or =
            (?:                                           # value alternatives
                "(?P<dqvalue>[^"]*)"                      #   double-quoted value
                |
                '(?P<sqvalue>[^']*)'                      #   single-quoted value
                |
                (?P<value>[^\s,)}]+)                      #   unquoted value
            )
            """,
        )

    def _is_secret_key_match(self, key: str) -> bool:
        """Check a key extracted from a log message against secret patterns."""
        return is_secret_key(key)

    def _redact_message(self, message: str) -> str:
        """Apply pattern-based redaction to a formatted log message."""
        result = message

        # 1. Redact known token formats (Bearer, GitHub PATs, etc.)
        for pattern, replacement in _TOKEN_LIKE_PATTERNS:
            result = pattern.sub(replacement, result)

        # 2. Redact KEY=VALUE / KEY:VALUE patterns where key is secret-like
        result = self._kv_pattern.sub(self._replace_match, result)

        return result

    def _replace_match(self, match: re.Match[str]) -> str:
        """Called for each key=value match — replace value if key is secret."""
        key = match.group("key")
        if not self._is_secret_key_match(key):
            return match.group(0)

        full = match.group(0)
        # Prefer the quoted groups; fall back to the unquoted group.
        value = (
            match.group("dqvalue")
            or match.group("sqvalue")
            or match.group("value")
        )
        if value is None:
            return full
        return full.replace(value, self._placeholder, 1)

    def format(self, record: logging.LogRecord) -> str:
        """Format the log record and redact any remaining secrets."""
        formatted = super().format(record)
        return self._redact_message(formatted)


def configure_root_logger(level: int = logging.INFO) -> None:
    """Install the :class:`RedactingFormatter` on the root logger.

    Removes any existing handlers from the root logger and attaches a
    single :class:`~logging.StreamHandler` that writes to stderr with
    the redacting formatter.

    Call this early in application startup (e.g. inside ``__main__.py``
    or ``create_app()``) to protect all log output.

    Args:
        level: Log level for the root logger (default ``INFO``).
    """
    root = logging.getLogger()
    # Remove existing handlers to avoid duplicate output
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(
        fmt="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    root.addHandler(handler)
    root.setLevel(level)
