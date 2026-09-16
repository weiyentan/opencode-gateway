"""Tests for the pure-domain replay merge policy module (issue #684).

Verifies the module boundary introduced by extracting the ADR 0012
replay-merge policy out of the DB/transaction orchestration module:

1. ``app.core.merge_policy`` exposes the full pure-domain API (delta
   field sets, ``DeltaResult``, ``IngestOutcome``, ``compute_delta``,
   ``validate_no_negative_totals``, ``_to_decimal``) and imports without
   pulling in ``asyncpg`` — the pure-domain constraint.
2. ``app.core.reconciliation`` re-exports those names **by identity**
   (the same objects, imported — not redefined) so every existing
   ``from app.core.reconciliation import ...`` caller keeps working.
3. The policy computes the ADR 0012 semantics from the new home
   (authoritative non-null values, non-erasing null/omitted values,
   session-token adjustment excluding ``reasoning_tokens``).
"""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal
from pathlib import Path

from app.core import merge_policy
from app.core.merge_policy import (
    COST_FIELD,
    DELTA_FIELDS,
    ROLLUP_FIELDS,
    SESSION_FIELD_MAP,
    SESSION_TOKEN_FIELDS,
    DeltaResult,
    IngestOutcome,
    _to_decimal,
    compute_delta,
    validate_no_negative_totals,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

# ══════════════════════════════════════════════════════════════════════════════
#  AC 1: the pure-domain API exists in merge_policy
# ══════════════════════════════════════════════════════════════════════════════


class TestMergePolicyApi:
    """The extracted pure-domain surface is importable from merge_policy."""

    def test_delta_field_sets_exist(self) -> None:
        assert DELTA_FIELDS == (
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "estimated_cost_usd",
        )
        assert ROLLUP_FIELDS == (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "estimated_cost_usd",
        )
        assert SESSION_TOKEN_FIELDS == (
            "input_tokens",
            "output_tokens",
            "cached_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        )
        assert COST_FIELD == "estimated_cost_usd"

    def test_session_field_map_exists(self) -> None:
        assert SESSION_FIELD_MAP == {
            "input_tokens": "total_input_tokens",
            "output_tokens": "total_output_tokens",
            "cached_tokens": "total_cached_tokens",
            "cache_read_tokens": "total_cache_read_tokens",
            "cache_write_tokens": "total_cache_write_tokens",
            "estimated_cost_usd": "total_estimated_cost_usd",
        }

    def test_ingest_outcome_vocabulary(self) -> None:
        assert [outcome.value for outcome in IngestOutcome] == [
            "accepted",
            "duplicate",
            "updated",
            "quarantined",
            "conflict",
            "rejected",
        ]

    def test_delta_result_construction(self) -> None:
        result = DeltaResult(
            old_values={"input_tokens": 1},
            new_values={"input_tokens": 2},
            deltas={"input_tokens": 1},
            token_adjustment=1,
            cost_adjustment=Decimal("0"),
        )
        assert result.token_adjustment == 1
        assert result.cost_adjustment == Decimal("0")

    def test_to_decimal(self) -> None:
        assert _to_decimal(None) is None
        assert _to_decimal(Decimal("1.5")) == Decimal("1.5")
        assert _to_decimal("2.25") == Decimal("2.25")
        assert _to_decimal(3) == Decimal("3")


# ══════════════════════════════════════════════════════════════════════════════
#  AC 1: pure-domain constraint — merge_policy must not import asyncpg
# ══════════════════════════════════════════════════════════════════════════════


class TestMergePolicyPurity:
    """Importing merge_policy never pulls in the DB driver."""

    def test_import_does_not_pull_asyncpg(self) -> None:
        code = (
            "import sys\n"
            "import app.core.merge_policy\n"
            "assert 'asyncpg' not in sys.modules, (\n"
            "    'merge_policy pulled in asyncpg: pure-domain constraint violated'\n"
            ")\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr


# ══════════════════════════════════════════════════════════════════════════════
#  AC 2 + 4: reconciliation re-exports the pure-domain names by identity
# ══════════════════════════════════════════════════════════════════════════════


class TestReconciliationImportBridge:
    """``from app.core.reconciliation import X`` keeps working — and the
    names are the *same objects* as merge_policy's (imported, not
    redefined), so there is exactly one source of truth."""

    def test_pure_domain_names_are_identity_reexports(self) -> None:
        import app.core.reconciliation as recon

        for name in (
            "DELTA_FIELDS",
            "ROLLUP_FIELDS",
            "SESSION_TOKEN_FIELDS",
            "COST_FIELD",
            "SESSION_FIELD_MAP",
            "DeltaResult",
            "IngestOutcome",
            "_to_decimal",
            "compute_delta",
            "validate_no_negative_totals",
        ):
            reexported = getattr(recon, name)
            canonical = getattr(merge_policy, name)
            assert reexported is canonical, (
                f"reconciliation.{name} is not the merge_policy object — "
                "the pure-domain definition leaked back into reconciliation"
            )


# ══════════════════════════════════════════════════════════════════════════════
#  AC 6: ADR 0012 semantics computed from the new home
# ══════════════════════════════════════════════════════════════════════════════


class TestComputeDeltaSemantics:
    """The policy functions behave identically from merge_policy."""

    def test_authoritative_non_null_value_produces_delta(self) -> None:
        old = {"input_tokens": 100, "output_tokens": 50}
        result = compute_delta(old, {"input_tokens": 150, "output_tokens": 50})
        assert result.deltas["input_tokens"] == 50
        assert result.deltas["output_tokens"] == 0
        assert result.token_adjustment == 50
        assert result.new_values["input_tokens"] == 150

    def test_null_incoming_value_never_erases(self) -> None:
        old = {"input_tokens": 100, "output_tokens": None}
        result = compute_delta(old, {"input_tokens": None, "output_tokens": 70})
        assert result.deltas["input_tokens"] == 0
        assert result.new_values["input_tokens"] == 100  # stored value kept
        assert result.deltas["output_tokens"] == 70

    def test_reasoning_delta_excluded_from_token_adjustment(self) -> None:
        old = {"reasoning_tokens": 10, "input_tokens": 0}
        result = compute_delta(old, {"reasoning_tokens": 25, "input_tokens": 5})
        assert result.deltas["reasoning_tokens"] == 15
        assert result.token_adjustment == 5  # reasoning_tokens not counted

    def test_cost_delta_uses_decimal_arithmetic(self) -> None:
        old = {"estimated_cost_usd": "0.0100"}
        result = compute_delta(old, {"estimated_cost_usd": "0.0250"})
        assert result.cost_adjustment == Decimal("0.0150")
        assert result.deltas["estimated_cost_usd"] == Decimal("0.0150")

    def test_validate_no_negative_totals_clamps_in_place(self) -> None:
        session_id = "00000000-0000-0000-0000-000000000001"
        adjusted = {
            "total_input_tokens": -5,
            "total_estimated_cost_usd": Decimal("-0.25"),
            "total_output_tokens": 10,
        }
        assert validate_no_negative_totals(session_id, adjusted) is True
        assert adjusted["total_input_tokens"] == 0
        assert adjusted["total_estimated_cost_usd"] == Decimal("0")
        assert adjusted["total_output_tokens"] == 10

    def test_validate_no_negative_totals_without_session(self) -> None:
        assert validate_no_negative_totals(None, {"total_input_tokens": -5}) is False
