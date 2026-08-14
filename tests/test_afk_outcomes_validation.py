"""Acceptance-criteria validation for AFK reconstruction (issue #450).

Locks the machine-checkable form of the #450 acceptance criteria against the
known-real #437-440 / #442 cluster fixture: the cluster reconstructs as ONE
AFK Run with a merged EngineeringOutcome, a newly-assigned ULID ``afk_run_id``,
``origin=reconstructed`` semantics (the run is produced by the resolver), and
explainable evidence on every link (method, confidence, source identifiers,
resolver_version).

This is the validation companion to ``docs/afk-outcome-validation.md``. It
drives the shipped :class:`CorrelationEngine` directly over the committed
real-data fixtures — it does not hand-roll a reconstruction (unlike
``test_afk_outcomes_fixtures.py``, which uses a synthetic builder).
"""

from __future__ import annotations

import json
from pathlib import Path

from afk_outcomes import (
    EngineeringOutcomeStatus,
    ResolutionResult,
    dumps_canonical,
)
from tests.test_afk_outcomes_correlation import (
    GITHUB_ULID_MS,
    _build_window,
    _engine,
    _load_raw,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "afk_outcomes"


async def _resolve_github() -> ResolutionResult:
    raw = _load_raw("github")
    seed, entities, events, sessions = _build_window(raw)
    return await _engine(GITHUB_ULID_MS).resolve(
        seed, entities=entities, events=events, sessions=sessions
    )


async def test_cluster_reconstructs_as_one_run_with_merged_outcome() -> None:
    """#437-440 / #442 reconstruct as ONE run with a merged EngineeringOutcome."""
    result = await _resolve_github()
    run = result.run

    # One run, no ambiguity or unmatched outcome.
    assert result.unresolved == []

    # Newly-assigned ULID afk_run_id (26 Crockford base32 characters).
    assert len(run.afk_run_id) == 26

    # Merged outcome bound to the consolidated change request.
    assert run.outcome is not None
    assert run.outcome.status is EngineeringOutcomeStatus.MERGED
    assert run.outcome.change_request_ids == ["change_request:442"]
    assert run.outcome.resolved_issue_ids == [
        "issue:437",
        "issue:438",
        "issue:439",
        "issue:440",
    ]
    assert run.outcome.merge_event_id == "merge_event:442"
    assert run.outcome.merged_at is not None


async def test_every_link_is_explainable() -> None:
    """Every link carries method, confidence, evidence, and resolver_version."""
    result = await _resolve_github()
    run = result.run

    assert run.correlations, "expected correlations"
    for correlation in run.correlations:
        assert correlation.method, "correlation missing method"
        assert correlation.correlation_confidence is not None
        assert correlation.resolver_version == "2"
        assert correlation.evidence, "correlation missing evidence"
        for evidence in correlation.evidence:
            assert evidence.source_entity_id, "evidence missing source identifier"

    for link in run.entity_links:
        assert link.resolver_version == "2"
        assert link.correlation_confidence is not None

    assert run.session_links, "expected a session link"
    for session_link in run.session_links:
        assert session_link.resolver_version == "2"
        assert session_link.inferred is True
        assert session_link.method is not None


async def test_noise_is_present_but_un_correlated() -> None:
    """Unrelated change_request #441 and issue #436 never enter the outcome."""
    result = await _resolve_github()
    run = result.run

    entity_ids = {entity.entity_id for entity in run.entities}
    assert "change_request:441" in entity_ids
    assert "issue:436" in entity_ids

    assert run.outcome is not None
    assert "change_request:441" not in run.outcome.change_request_ids
    assert "issue:436" not in run.outcome.resolved_issue_ids

    noise_roles = {
        link.entity_id: link.role
        for link in run.entity_links
        if link.role == "noise"
    }
    assert noise_roles.get("change_request:441") == "noise"

    mention = [
        c
        for c in run.correlations
        if c.entity_id == "issue:436" and c.correlation_confidence == 0.1
    ]
    assert len(mention) == 1


async def test_reconstruction_is_deterministic_and_matches_golden() -> None:
    """Same fixture, twice, produces byte-identical canonical output matching golden."""
    first = dumps_canonical((await _resolve_github()).run)
    second = dumps_canonical((await _resolve_github()).run)
    assert first == second

    golden = (FIXTURES_DIR / "github" / "golden_resolution.json").read_text(encoding="utf-8").rstrip("\n")
    golden_run = json.loads(golden)["data"]["run"]
    assert json.loads(first)["data"] == golden_run
