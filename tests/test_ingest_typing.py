"""Strict-mypy regression guard for the collector ingest slice (issue #829).

Behavioural coverage for the collector ingress path (authorised/rejected
collectors, empty-batch heartbeats, partial success and conflicts) lives in
``tests/test_ingest.py`` and is intentionally reused here.  This module
exercises the *typing* contract instead: it runs mypy against
``app/api/ingest.py`` using the isolated configuration (the shipped
configuration minus the module-wide ``app.api.ingest`` ``ignore_errors``
exemption) and asserts that only the diagnostics documented as owned by the
later persistence/enrichment slices remain.

Collector authentication, empty-heartbeat, request-validation and per-record
result diagnostics must be eliminated in this slice — a diagnostic outside the
documented later-slice allowlist fails this test.

See ``docs/ingest-typing/remaining-diagnostics.md`` for the inventory and the
owning slices.  If a later slice fixes one of the documented diagnostics the
corresponding count simply drops to zero; extend the allowlist only when a
diagnostic genuinely belongs to a later slice.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "mypy-strict-ingest.ini"
TARGET = "app/api/ingest.py"

#: Diagnostics that remain while the shipped ``app.api.ingest`` exemption is
#: still in place.  Each entry is ``(error_code, message_pattern, max_count)``
#: and belongs to a later slice: ``#830`` owns persistence, ``#831`` owns
#: enrichment (and the eventual exemption removal).
DOCUMENTED_REMAINING: tuple[tuple[str, str, int], ...] = (
    # #831 — cache-token delta casts (enrichment)
    ("call-overload", r'No overload variant of "int" matches argument type "object"', 2),
    # #830 — UUID returns from asyncpg rows (persistence)
    ("no-any-return", r'Returning Any from function declared to return "UUID"', 2),
    # #831 — JSONB serialization narrowing (enrichment)
    (
        "assignment",
        r'Incompatible types in assignment \(expression has type "str \| None", '
        r'variable has type "dict\[Any, Any\] \| None"\)',
        2,
    ),
    # #830 — duplicate ``new_values`` binding in the replay-merge branches
    # (persistence); the message embeds a line number, so match the prefix.
    ("no-redef", r'Name "new_values" already defined', 1),
)


def _run_isolated_mypy(cache_dir: Path) -> list[dict[str, Any]]:
    """Run the isolated strict-mypy check and return its error records."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(CONFIG_PATH),
            TARGET,
            "--output=json",
            "--no-error-summary",
            "--no-incremental",
            "--cache-dir",
            str(cache_dir),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    # mypy exits 1 when it reports type errors; that is the expected state
    # while later slices still own diagnostics.
    assert completed.returncode in (0, 1), (
        "isolated mypy invocation failed:\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )

    errors: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        payload = json.loads(line)
        if payload.get("severity") == "error":
            errors.append(payload)
    return errors


def test_isolated_strict_mypy_only_reports_later_slice_diagnostics(tmp_path: Path) -> None:
    errors = _run_isolated_mypy(tmp_path / "mypy-cache")

    unmatched = []
    for error in errors:
        code = error.get("code")
        message = error.get("message", "")
        is_documented = any(
            code == documented_code and re.search(pattern, message)
            for documented_code, pattern, _ in DOCUMENTED_REMAINING
        )
        if not is_documented:
            unmatched.append(f"{code}: {message} (line {error.get('line')})")

    assert not unmatched, (
        "strict-mypy diagnostics outside the documented later-slice inventory "
        "(collector auth / heartbeat / request validation / per-record result "
        "diagnostics must be eliminated by #829):\n" + "\n".join(unmatched)
    )

    for code, pattern, max_count in DOCUMENTED_REMAINING:
        actual = sum(
            1
            for error in errors
            if error.get("code") == code and re.search(pattern, error.get("message", ""))
        )
        assert actual <= max_count, (
            f"expected at most {max_count} documented '{code}' diagnostic(s) "
            f"matching {pattern!r}, found {actual}"
        )
