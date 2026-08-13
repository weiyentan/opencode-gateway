"""Deterministic AFK correlation engine tests.

Drives the :class:`CorrelationEngine` with the real historical #444 fixtures
and with synthetic windows that exercise rule ordering, multi-issue binding,
ambiguity, unmatched surfacing, temporal inference, explicit run-id binding,
and provisional session attachment.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from afk_outcomes import (
    AFKRun,
    Correlation,
    CorrelationEngine,
    EngineeringEntity,
    EngineeringEvent,
    EntityType,
    Provider,
    ResolutionResult,
    RunStatus,
    SequenceULID,
    SessionDescriptor,
    UnresolvedReason,
    dumps_canonical,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "afk_outcomes"

GITHUB_ULID_MS = 1_786_615_829_000  # 2026-08-13T10:10:29Z
GITLAB_ULID_MS = 1_786_102_200_000  # 2026-08-07T11:30:00Z

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ── Historical fixture normalisation: raw provider payload → engine window ──


def _build_window(
    raw: dict,
) -> tuple[
    AFKRun,
    list[EngineeringEntity],
    list[EngineeringEvent],
    list[SessionDescriptor],
]:
    provider = Provider(raw["provider"])
    repository = raw["repository"]
    run_meta = raw["run"]

    seed = AFKRun(
        afk_run_id="",
        provider=provider,
        status=RunStatus(run_meta["status"]),
        title=run_meta["title"],
        started_at=_parse_dt(run_meta["started_at"]),
        finished_at=_parse_dt(run_meta["finished_at"]),
    )
    session = SessionDescriptor(
        session_id=run_meta.get("session_id"),
        external_session_id=run_meta.get("external_session_id"),
        started_at=_parse_dt(run_meta["started_at"]),
        finished_at=_parse_dt(run_meta["finished_at"]),
    )

    entities: list[EngineeringEntity] = []
    events: list[EngineeringEvent] = []

    def add_event(entity_id: str, event_type: str, occurred_at: str, actor: str | None,
                  payload: dict | None = None) -> None:
        events.append(
            EngineeringEvent(
                event_id=f"{entity_id}:{event_type}",
                event_type=event_type,
                provider=provider,
                entity_id=entity_id,
                occurred_at=_parse_dt(occurred_at),
                actor=actor,
                payload=payload or {},
            )
        )

    is_gitlab = provider is Provider.GITLAB

    # Issues
    for issue in raw["issues"]:
        number = issue["iid"] if is_gitlab else issue["number"]
        author = issue["author"]["username"] if is_gitlab else issue["user"]["login"]
        url = issue["web_url"] if is_gitlab else issue["html_url"]
        entity_id = f"issue:{number}"
        entities.append(
            EngineeringEntity(
                entity_id=entity_id,
                entity_type=EntityType.ISSUE,
                provider=provider,
                repository=repository,
                number=number,
                title=issue["title"],
                state=issue["state"],
                author=author,
                url=url,
                created_at=_parse_dt(issue["created_at"]),
            )
        )
        add_event(entity_id, "opened", issue["created_at"], author)
        if issue.get("closed_at"):
            add_event(entity_id, "closed", issue["closed_at"], author)

    # Change requests
    crs = raw["merge_requests"] if is_gitlab else raw["change_requests"]
    for cr in crs:
        number = cr["iid"] if is_gitlab else cr["number"]
        author = cr["author"]["username"] if is_gitlab else cr["user"]["login"]
        url = cr["web_url"] if is_gitlab else cr["html_url"]
        branch = cr["source_branch"] if is_gitlab else cr["head"]["ref"]
        description = cr.get("description", "") if is_gitlab else cr.get("body", "")
        entity_id = f"change_request:{number}"
        entities.append(
            EngineeringEntity(
                entity_id=entity_id,
                entity_type=EntityType.CHANGE_REQUEST,
                provider=provider,
                repository=repository,
                number=number,
                title=cr["title"],
                state=cr["state"],
                author=author,
                url=url,
                created_at=_parse_dt(cr["created_at"]),
                description=description,
                branch=branch,
            )
        )
        add_event(entity_id, "opened", cr["created_at"], author)

    # Commits
    for commit in raw["commits"]:
        sha = commit["id"] if is_gitlab else commit["sha"]
        message = commit["title"] if is_gitlab else commit["message"]
        author = commit["author_name"] if is_gitlab else commit["author"]["name"]
        date = commit["created_at"] if is_gitlab else commit["author"]["date"]
        entity_id = f"commit:{sha}"
        entities.append(
            EngineeringEntity(
                entity_id=entity_id,
                entity_type=EntityType.COMMIT,
                provider=provider,
                repository=repository,
                title=message,
                author=author,
                created_at=_parse_dt(date),
            )
        )
        add_event(entity_id, "committed", date, author, payload={"sha": sha, "message": message})

    # Reviews
    for review in raw["reviews"]:
        author = review["author"]["username"] if is_gitlab else review["user"]["login"]
        entity_id = f"review:{review['id']}"
        entities.append(
            EngineeringEntity(
                entity_id=entity_id,
                entity_type=EntityType.REVIEW,
                provider=provider,
                repository=repository,
                title=f"review {review['state']}",
                author=author,
                created_at=_parse_dt(review["submitted_at"]),
            )
        )
        add_event(entity_id, "review_submitted", review["submitted_at"], author,
                  payload={"state": review["state"], "commit_id": review["commit_id"]})

    # Merge events
    for merge in raw["merge_events"]:
        number = merge["merge_request_iid"] if is_gitlab else merge["change_request_number"]
        entity_id = f"merge_event:{number}"
        entities.append(
            EngineeringEntity(
                entity_id=entity_id,
                entity_type=EntityType.MERGE_EVENT,
                provider=provider,
                repository=repository,
                number=number,
                title=f"merge {number}",
                author=merge["actor"],
                created_at=_parse_dt(merge["merged_at"]),
            )
        )
        add_event(entity_id, "merged", merge["merged_at"], merge["actor"],
                  payload={"commit_sha": merge["commit_sha"]})

    return seed, entities, events, [session]


def _load_raw(provider: str) -> dict:
    return json.loads((FIXTURES_DIR / provider / "raw_payload.json").read_text())


def _engine(ulid_ms: int) -> CorrelationEngine:
    return CorrelationEngine(ulid_source=SequenceULID(timestamp_ms=ulid_ms))


async def _resolve(provider: str) -> ResolutionResult:
    raw = _load_raw(provider)
    seed, entities, events, sessions = _build_window(raw)
    ulid_ms = GITHUB_ULID_MS if provider == "github" else GITLAB_ULID_MS
    return await _engine(ulid_ms).resolve(
        seed, entities=entities, events=events, sessions=sessions
    )


# ── Acceptance criteria ──────────────────────────────────────────────────────


@pytest.mark.parametrize("provider", ["github", "gitlab"])
async def test_golden_determinism_byte_identical(provider: str) -> None:
    """Same fixture, run twice, produces byte-identical canonical output."""
    first = dumps_canonical(await _resolve(provider))
    second = dumps_canonical(await _resolve(provider))
    assert first == second

    golden = (FIXTURES_DIR / provider / "golden_resolution.json").read_text().rstrip("\n")
    assert first == golden, "engine canonical output drifted from golden_resolution.json"


async def test_github_multi_issue_binding_is_not_ambiguous() -> None:
    result = await _resolve("github")
    resolved = [
        c for c in result.run.correlations
        if c.entity_id.startswith("issue:") and c.correlation_confidence >= 0.5
    ]
    resolved_ids = sorted(c.entity_id for c in resolved)
    assert resolved_ids == ["issue:437", "issue:438", "issue:439", "issue:440"]
    # Multi-issue binding must not surface as ambiguity.
    assert not result.unresolved, f"expected no unresolved, got {result.unresolved}"


async def test_every_link_carries_method_confidence_evidence_and_version() -> None:
    result = await _resolve("github")
    assert result.run.correlations, "expected correlations"
    for correlation in result.run.correlations:
        assert correlation.method, "missing method"
        assert correlation.correlation_confidence is not None
        assert correlation.resolver_version == "1"
        assert correlation.evidence, "missing evidence"
        for evidence in correlation.evidence:
            assert evidence.source_entity_id, "evidence missing source identifier"


async def test_session_links_are_inferred_and_provisional() -> None:
    result = await _resolve("github")
    assert result.run.session_links, "expected a session link"
    for link in result.run.session_links:
        assert link.inferred is True
        assert link.method == "temporal_overlap"


async def test_rule_ordering_lower_confidence_never_overrides() -> None:
    """An issue locked by issue_reference (1.0) must not be re-bound by a
    lower-confidence commit_issue_reference (0.6)."""
    run = AFKRun(
        afk_run_id="",
        provider=Provider.GITHUB,
        status=RunStatus.COMPLETED,
        title="Implement issue #100",
        started_at=_parse_dt("2026-08-13T08:00:00Z"),
        finished_at=_parse_dt("2026-08-13T10:00:00Z"),
    )
    cr = EngineeringEntity(
        entity_id="change_request:500",
        entity_type=EntityType.CHANGE_REQUEST,
        provider=Provider.GITHUB,
        repository="repo",
        number=500,
        title="Implement issue #100",
        state="merged",
        author="alice",
        created_at=_parse_dt("2026-08-13T08:00:00Z"),
        description="Resolves #100.",
        branch="ai/feat/issue-100",
    )
    issue = EngineeringEntity(
        entity_id="issue:100",
        entity_type=EntityType.ISSUE,
        provider=Provider.GITHUB,
        repository="repo",
        number=100,
        title="Do the thing",
        state="closed",
        author="alice",
        created_at=_parse_dt("2026-08-10T09:00:00Z"),
    )
    commit = EngineeringEntity(
        entity_id="commit:abc123",
        entity_type=EntityType.COMMIT,
        provider=Provider.GITHUB,
        repository="repo",
        title="wip: #100 (draft reference)",
        author="alice",
        created_at=_parse_dt("2026-08-13T09:00:00Z"),
    )
    engine = _engine(1_786_615_829_000)
    result = await engine.resolve(
        run, entities=[issue, cr, commit], events=[], sessions=[]
    )

    issue_correlations = [c for c in result.run.correlations if c.entity_id == "issue:100"]
    assert len(issue_correlations) == 1, "issue:100 must be locked exactly once"
    assert issue_correlations[0].method == "issue_reference"
    assert issue_correlations[0].correlation_confidence == 1.0


async def test_ambiguous_surfaces_resolver_result_not_forced_link() -> None:
    """Two change requests with the run title → ambiguity, never a forced link."""
    run = AFKRun(
        afk_run_id="",
        provider=Provider.GITHUB,
        status=RunStatus.COMPLETED,
        title="Implement issue #100",
        started_at=_parse_dt("2026-08-13T08:00:00Z"),
        finished_at=_parse_dt("2026-08-13T10:00:00Z"),
    )
    crs = [
        EngineeringEntity(
            entity_id=f"change_request:{n}",
            entity_type=EntityType.CHANGE_REQUEST,
            provider=Provider.GITHUB,
            repository="repo",
            number=n,
            title="Implement issue #100",
            state="merged",
            author="alice",
            created_at=_parse_dt("2026-08-13T08:00:00Z"),
            description="Resolves #100.",
            branch="ai/feat/issue-100",
        )
        for n in (500, 501)
    ]
    engine = _engine(1_786_615_829_000)
    result = await engine.resolve(run, entities=crs, events=[], sessions=[])

    assert not result.run.correlations, "ambiguity must not force a link"
    assert len(result.unresolved) == 1
    unresolved = result.unresolved[0]
    assert unresolved.reason is UnresolvedReason.AMBIGUOUS
    assert sorted(unresolved.candidates) == ["change_request:500", "change_request:501"]


async def test_unmatched_surfaces_resolver_result() -> None:
    """A run with no owning change request and no links → unmatched result."""
    run = AFKRun(
        afk_run_id="",
        provider=Provider.GITHUB,
        status=RunStatus.COMPLETED,
        title="Something no change request matches",
        started_at=_parse_dt("2026-08-13T08:00:00Z"),
        finished_at=_parse_dt("2026-08-13T10:00:00Z"),
    )
    unrelated = EngineeringEntity(
        entity_id="issue:999",
        entity_type=EntityType.ISSUE,
        provider=Provider.GITHUB,
        repository="repo",
        number=999,
        title="unrelated",
        state="open",
        author="bob",
        created_at=_parse_dt("2026-07-01T00:00:00Z"),
    )
    engine = _engine(1_786_615_829_000)
    result = await engine.resolve(run, entities=[unrelated], events=[], sessions=[])

    assert not result.run.correlations
    assert len(result.unresolved) == 1
    assert result.unresolved[0].reason is UnresolvedReason.UNMATCHED


async def test_temporal_inference_binds_overlapping_entity() -> None:
    run = AFKRun(
        afk_run_id="",
        provider=Provider.GITHUB,
        status=RunStatus.COMPLETED,
        title="No change request",
        started_at=_parse_dt("2026-08-13T08:00:00Z"),
        finished_at=_parse_dt("2026-08-13T10:00:00Z"),
    )
    overlapping = EngineeringEntity(
        entity_id="issue:300",
        entity_type=EntityType.ISSUE,
        provider=Provider.GITHUB,
        repository="repo",
        number=300,
        title="overlaps the run window",
        state="closed",
        author="carol",
        created_at=_parse_dt("2026-08-13T07:00:00Z"),
        updated_at=_parse_dt("2026-08-13T09:00:00Z"),
    )
    engine = _engine(1_786_615_829_000)
    result = await engine.resolve(run, entities=[overlapping], events=[], sessions=[])

    temporal = [c for c in result.run.correlations if c.method == "temporal_inference"]
    assert temporal, "expected a temporal_inference link"
    assert temporal[0].entity_id == "issue:300"
    assert temporal[0].correlation_confidence == 0.4


async def test_explicit_run_id_binds_via_event_payload() -> None:
    run = AFKRun(
        afk_run_id="01KZX9M4G80000000000000000",
        provider=Provider.GITHUB,
        status=RunStatus.COMPLETED,
        title="Explicit run",
        started_at=_parse_dt("2026-08-13T08:00:00Z"),
        finished_at=_parse_dt("2026-08-13T10:00:00Z"),
    )
    entity = EngineeringEntity(
        entity_id="change_request:700",
        entity_type=EntityType.CHANGE_REQUEST,
        provider=Provider.GITHUB,
        repository="repo",
        number=700,
        title="Explicit run",
        state="merged",
        author="dave",
        created_at=_parse_dt("2026-08-13T08:00:00Z"),
        description="",
        branch="ai/feat/x",
    )
    event = EngineeringEvent(
        event_id="evt:1",
        event_type="committed",
        provider=Provider.GITHUB,
        entity_id="change_request:700",
        occurred_at=_parse_dt("2026-08-13T08:05:00Z"),
        actor="dave",
        payload={"run_id": "01KZX9M4G80000000000000000"},
    )
    engine = _engine(1_786_615_829_000)
    result = await engine.resolve(run, entities=[entity], events=[event], sessions=[])

    explicit = [c for c in result.run.correlations if c.method == "explicit_run_id"]
    assert explicit, "expected an explicit_run_id link"
    assert explicit[0].entity_id == "change_request:700"


async def test_rules_satisfy_correlation_rule_protocol() -> None:
    """Each concrete rule exposes an async ``correlate`` returning Correlations."""
    from afk_outcomes import (
        BranchIssueReferenceRule,
        CommitIssueReferenceRule,
        ExplicitRunIdRule,
        IssueReferenceRule,
        TemporalInferenceRule,
    )

    run = AFKRun(
        afk_run_id="r1",
        provider=Provider.GITHUB,
        status=RunStatus.COMPLETED,
        title="Implement issue #100",
        started_at=_parse_dt("2026-08-13T08:00:00Z"),
        finished_at=_parse_dt("2026-08-13T10:00:00Z"),
    )
    for rule in (
        ExplicitRunIdRule(),
        IssueReferenceRule(),
        BranchIssueReferenceRule(),
        CommitIssueReferenceRule(),
        TemporalInferenceRule(),
    ):
        out = await rule.correlate(run, entities=[], events=[])
        assert isinstance(out, list)
        assert all(isinstance(c, Correlation) for c in out)
