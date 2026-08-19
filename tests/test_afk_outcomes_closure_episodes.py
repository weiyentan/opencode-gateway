"""Closure-episode projection tests (issue #524) — pure domain.

Proves the acceptance criteria of the closure-episode projector:

* a same-repo change request whose merge precedes the issue close and whose
  declaration is active yields exactly one episode with status ``inferred``
  attributing that change request;
* cross-repo ``group/project#N`` references keep independent change-request
  and issue repository keys;
* an ``issue_links`` snapshot diff that removes a previously declared
  closure produces an explicit revocation and the episode recomputes
  without attributing the removed declaration;
* zero candidates report ``unmatched``, multiple candidates ``ambiguous`` —
  both as versioned unresolved records, never arbitrarily tie-broken;
* ordering policy D: facts project by provider ``occurred_at`` (never
  ingestion order), identical same-timestamp snapshots coalesce,
  conflicting same-timestamp snapshots park unresolved, and webhook
  outranks backfill at equal timestamps;
* reopen/reclose produces two immutable episodes — the earlier
  ``superseded``, the current projection pointing at the latest;
* the open-interval statuses ``pending`` / ``awaiting_closure``;
* determinism: identical input produces identical output in any order.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from afk_outcomes.closure_episodes import (
    CLOSURE_RESOLVER_VERSION,
    ClosureFact,
    project_closure_episodes,
)
from afk_outcomes.models import (
    ClosureEpisodeStatus,
    ClosureLinkKind,
    ClosureLinkState,
    EntityType,
    IssueLinkTarget,
    IssueLinksSnapshot,
    Provider,
)

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+

T0 = datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)

# ── helpers ─────────────────────────────────────────────────────────────────


def _t(seconds: int) -> datetime:
    """A tz-aware timestamp T0 + seconds (monotonic test timeline)."""
    return T0 + timedelta(seconds=seconds)


def _snapshot(
    *,
    declares: list[tuple[str, str]] | None = None,
    references: list[tuple[str, str]] | None = None,
) -> IssueLinksSnapshot:
    """Build an issue_links snapshot from (repository, number) pairs."""
    return IssueLinksSnapshot(
        declares_closure=[
            IssueLinkTarget(repository=repository, number=number)
            for repository, number in (declares or [])
        ],
        references=[
            IssueLinkTarget(repository=repository, number=number)
            for repository, number in (references or [])
        ],
    )


def _fact(
    *,
    event_type: str,
    external_id: str,
    repository: str,
    provider: Provider = Provider.GITLAB,
    occurred_at: datetime,
    observed_via: str = "webhook",
    issue_links: IssueLinksSnapshot | None = None,
) -> ClosureFact:
    """Build one normalized closure fact (entity type derived from event type)."""
    entity_type = (
        EntityType.ISSUE
        if event_type.startswith("issue.")
        else EntityType.CHANGE_REQUEST
    )
    return ClosureFact(
        provider=provider,
        repository=repository,
        entity_type=entity_type,
        external_id=external_id,
        event_type=event_type,
        occurred_at=occurred_at,
        observed_via=observed_via,
        issue_links=issue_links,
    )


def _episode(
    projection,
    *,
    issue_repository: str,
    issue_external_id: str,
):
    """Return the single episode for an issue (any status)."""
    matches = [
        ep
        for ep in projection.episodes
        if ep.issue_repository == issue_repository
        and ep.issue_external_id == issue_external_id
    ]
    return matches


def _link_states(projection, *, kind: ClosureLinkKind) -> dict[tuple[str, str], str]:
    """Return {(issue_repository, issue_external_id): state} per link kind."""
    return {
        (link.issue_repository, link.issue_external_id): link.state.value
        for link in projection.links
        if link.kind is kind
    }


# ── AC1: same-repo inferred attribution ──────────────────────────────────────


def test_same_repo_merged_before_close_yields_single_inferred_episode() -> None:
    """A GitLab MR with closing syntax, merged before the close, yields exactly
    one inferred episode attributing that MR."""
    repo = "gitlab.com/cloudnative-pg"
    facts = [
        _fact(
            event_type="change_request.opened",
            external_id="6",
            repository=repo,
            occurred_at=_t(0),
            issue_links=_snapshot(declares=[(repo, "1")]),
        ),
        _fact(
            event_type="change_request.merged",
            external_id="6",
            repository=repo,
            occurred_at=_t(10),
        ),
        _fact(
            event_type="issue.closed",
            external_id="1",
            repository=repo,
            occurred_at=_t(20),
        ),
    ]

    projection = project_closure_episodes(facts)

    episodes = _episode(projection, issue_repository=repo, issue_external_id="1")
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode.status is ClosureEpisodeStatus.INFERRED
    assert episode.change_request_repository == repo
    assert episode.change_request_external_id == "6"
    assert episode.closed_at == _t(20)
    assert episode.resolver_version == CLOSURE_RESOLVER_VERSION
    assert projection.unresolved == []
    # the declaration link is active and recorded as a distinct kind
    assert _link_states(projection, kind=ClosureLinkKind.DECLARES_CLOSURE) == {
        (repo, "1"): ClosureLinkState.ACTIVE.value
    }


# ── AC2: cross-repo independent repository keys ──────────────────────────────


def test_cross_repo_declaration_keeps_independent_repository_keys() -> None:
    """A group/project#N declaration keeps the change-request repository and the
    issue repository as two independent keys, and attributes correctly."""
    cr_repo = "gitlab.com/application/api"
    issue_repo = "gitlab.com/platform/tracking"
    facts = [
        _fact(
            event_type="change_request.opened",
            external_id="10",
            repository=cr_repo,
            occurred_at=_t(0),
            issue_links=_snapshot(declares=[(issue_repo, "25")]),
        ),
        _fact(
            event_type="change_request.merged",
            external_id="10",
            repository=cr_repo,
            occurred_at=_t(10),
        ),
        _fact(
            event_type="issue.closed",
            external_id="25",
            repository=issue_repo,
            occurred_at=_t(20),
        ),
    ]

    projection = project_closure_episodes(facts)

    episodes = _episode(
        projection, issue_repository=issue_repo, issue_external_id="25"
    )
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode.status is ClosureEpisodeStatus.INFERRED
    # the two endpoints carry independent repository keys
    assert episode.issue_repository == issue_repo
    assert episode.change_request_repository == cr_repo
    assert episode.change_request_external_id == "10"


# ── AC3: snapshot-diff revocation ────────────────────────────────────────────


def test_snapshot_diff_revokes_declaration_and_episode_drops_attribution() -> None:
    """Removing a previously declared closure from the next snapshot produces an
    explicit revocation; the recomputed episode no longer attributes it."""
    repo = "gitlab.com/cloudnative-pg"
    other_repo = "gitlab.com/other/project"
    facts = [
        _fact(
            event_type="change_request.opened",
            external_id="6",
            repository=repo,
            occurred_at=_t(0),
            issue_links=_snapshot(declares=[(repo, "1")]),
        ),
        # next snapshot drops the #1 declaration (adds an unrelated one)
        _fact(
            event_type="change_request.updated",
            external_id="6",
            repository=repo,
            occurred_at=_t(5),
            issue_links=_snapshot(declares=[(other_repo, "9")]),
        ),
        _fact(
            event_type="change_request.merged",
            external_id="6",
            repository=repo,
            occurred_at=_t(10),
        ),
        _fact(
            event_type="issue.closed",
            external_id="1",
            repository=repo,
            occurred_at=_t(20),
        ),
    ]

    projection = project_closure_episodes(facts)

    # explicit revocation: the removed declaration is revoked, never re-attributed
    assert _link_states(projection, kind=ClosureLinkKind.DECLARES_CLOSURE) == {
        (repo, "1"): ClosureLinkState.REVOKED.value,
        (other_repo, "9"): ClosureLinkState.ACTIVE.value,
    }
    episodes = _episode(projection, issue_repository=repo, issue_external_id="1")
    assert len(episodes) == 1
    assert episodes[0].status is ClosureEpisodeStatus.UNMATCHED
    assert episodes[0].change_request_external_id is None
    # the unmatched outcome is a versioned unresolved record
    assert len(projection.unresolved) == 1
    record = projection.unresolved[0]
    assert record.reason == "unmatched"
    assert record.candidates == []


def test_revoked_declaration_can_be_redeclared() -> None:
    """A later snapshot re-declaring the removed link re-activates it."""
    repo = "gitlab.com/cloudnative-pg"
    facts = [
        _fact(
            event_type="change_request.opened",
            external_id="6",
            repository=repo,
            occurred_at=_t(0),
            issue_links=_snapshot(declares=[(repo, "1")]),
        ),
        _fact(
            event_type="change_request.updated",
            external_id="6",
            repository=repo,
            occurred_at=_t(5),
            issue_links=_snapshot(declares=[]),
        ),
        _fact(
            event_type="change_request.updated",
            external_id="6",
            repository=repo,
            occurred_at=_t(8),
            issue_links=_snapshot(declares=[(repo, "1")]),
        ),
        _fact(
            event_type="change_request.merged",
            external_id="6",
            repository=repo,
            occurred_at=_t(10),
        ),
        _fact(
            event_type="issue.closed",
            external_id="1",
            repository=repo,
            occurred_at=_t(20),
        ),
    ]

    projection = project_closure_episodes(facts)

    assert _link_states(projection, kind=ClosureLinkKind.DECLARES_CLOSURE) == {
        (repo, "1"): ClosureLinkState.ACTIVE.value
    }
    episodes = _episode(projection, issue_repository=repo, issue_external_id="1")
    assert episodes[0].status is ClosureEpisodeStatus.INFERRED


# ── AC4: zero/multiple candidates — versioned unresolved, never tie-broken ───


def test_closed_with_zero_candidates_is_unmatched_with_unresolved_record() -> None:
    """An issue closed with no eligible candidates reports unmatched and emits a
    versioned unresolved record."""
    repo = "gitlab.com/cloudnative-pg"
    facts = [
        _fact(
            event_type="issue.closed",
            external_id="7",
            repository=repo,
            occurred_at=_t(20),
        ),
    ]

    projection = project_closure_episodes(facts)

    episodes = _episode(projection, issue_repository=repo, issue_external_id="7")
    assert len(episodes) == 1
    assert episodes[0].status is ClosureEpisodeStatus.UNMATCHED
    assert episodes[0].change_request_external_id is None
    assert len(projection.unresolved) == 1
    record = projection.unresolved[0]
    assert record.reason == "unmatched"
    assert record.candidates == []
    assert record.closed_at == _t(20)
    assert record.resolver_version == CLOSURE_RESOLVER_VERSION


def test_closed_with_multiple_candidates_is_ambiguous_never_tie_broken() -> None:
    """Two merged declaring change requests yield ambiguous — never an arbitrary
    winner — with both candidates in the versioned unresolved record."""
    repo = "gitlab.com/cloudnative-pg"
    facts = [
        _fact(
            event_type="change_request.opened",
            external_id="6",
            repository=repo,
            occurred_at=_t(0),
            issue_links=_snapshot(declares=[(repo, "1")]),
        ),
        _fact(
            event_type="change_request.merged",
            external_id="6",
            repository=repo,
            occurred_at=_t(10),
        ),
        _fact(
            event_type="change_request.opened",
            external_id="8",
            repository=repo,
            occurred_at=_t(5),
            issue_links=_snapshot(declares=[(repo, "1")]),
        ),
        _fact(
            event_type="change_request.merged",
            external_id="8",
            repository=repo,
            occurred_at=_t(15),
        ),
        _fact(
            event_type="issue.closed",
            external_id="1",
            repository=repo,
            occurred_at=_t(20),
        ),
    ]

    projection = project_closure_episodes(facts)

    episodes = _episode(projection, issue_repository=repo, issue_external_id="1")
    assert len(episodes) == 1
    assert episodes[0].status is ClosureEpisodeStatus.AMBIGUOUS
    # never an arbitrary winner — no attribution is fabricated
    assert episodes[0].change_request_external_id is None
    assert len(projection.unresolved) == 1
    record = projection.unresolved[0]
    assert record.reason == "ambiguous"
    candidate_ids = sorted(c.external_id for c in record.candidates)
    assert candidate_ids == ["6", "8"]
    assert all(c.repository == repo for c in record.candidates)


def test_merge_strictly_before_close_is_required_for_eligibility() -> None:
    """A merge at or after the close observation does not make a candidate —
    the inference is strict (merge precedes the close)."""
    repo = "gitlab.com/cloudnative-pg"
    facts = [
        _fact(
            event_type="change_request.opened",
            external_id="6",
            repository=repo,
            occurred_at=_t(0),
            issue_links=_snapshot(declares=[(repo, "1")]),
        ),
        _fact(
            event_type="issue.closed",
            external_id="1",
            repository=repo,
            occurred_at=_t(10),
        ),
        _fact(
            event_type="change_request.merged",
            external_id="6",
            repository=repo,
            occurred_at=_t(20),  # after the close
        ),
    ]

    projection = project_closure_episodes(facts)

    episodes = _episode(projection, issue_repository=repo, issue_external_id="1")
    assert episodes[0].status is ClosureEpisodeStatus.UNMATCHED


# ── AC5: ordering policy D ───────────────────────────────────────────────────


def test_out_of_order_facts_project_by_occurred_at() -> None:
    """Reversing the fact delivery order never changes the projection — the
    projector orders by provider occurred_at, not ingestion order."""
    repo = "gitlab.com/cloudnative-pg"
    facts = [
        _fact(
            event_type="change_request.opened",
            external_id="6",
            repository=repo,
            occurred_at=_t(0),
            issue_links=_snapshot(declares=[(repo, "1")]),
        ),
        _fact(
            event_type="change_request.merged",
            external_id="6",
            repository=repo,
            occurred_at=_t(10),
        ),
        _fact(
            event_type="issue.closed",
            external_id="1",
            repository=repo,
            occurred_at=_t(20),
        ),
    ]

    forward = project_closure_episodes(facts)
    backward = project_closure_episodes(list(reversed(facts)))

    assert forward.model_dump(mode="json") == backward.model_dump(mode="json")
    episodes = _episode(forward, issue_repository=repo, issue_external_id="1")
    assert episodes[0].status is ClosureEpisodeStatus.INFERRED


def test_identical_same_timestamp_snapshots_are_harmless() -> None:
    """Two facts at the same occurred_at carrying identical snapshots coalesce —
    no conflict, no park, a single active link."""
    repo = "gitlab.com/cloudnative-pg"
    facts = [
        _fact(
            event_type="change_request.opened",
            external_id="6",
            repository=repo,
            occurred_at=_t(0),
            issue_links=_snapshot(declares=[(repo, "1")]),
        ),
        _fact(
            event_type="change_request.updated",
            external_id="6",
            repository=repo,
            occurred_at=_t(0),  # same timestamp, identical snapshot
            issue_links=_snapshot(declares=[(repo, "1")]),
        ),
        _fact(
            event_type="change_request.merged",
            external_id="6",
            repository=repo,
            occurred_at=_t(10),
        ),
        _fact(
            event_type="issue.closed",
            external_id="1",
            repository=repo,
            occurred_at=_t(20),
        ),
    ]

    projection = project_closure_episodes(facts)

    assert _link_states(projection, kind=ClosureLinkKind.DECLARES_CLOSURE) == {
        (repo, "1"): ClosureLinkState.ACTIVE.value
    }
    episodes = _episode(projection, issue_repository=repo, issue_external_id="1")
    assert episodes[0].status is ClosureEpisodeStatus.INFERRED


def test_conflicting_same_timestamp_snapshots_park_unresolved() -> None:
    """Differing same-source snapshots at one occurred_at park unresolved —
    neither snapshot is arbitrarily won, and the episode stays ambiguous."""
    repo = "gitlab.com/cloudnative-pg"
    other_repo = "gitlab.com/other/project"
    facts = [
        _fact(
            event_type="change_request.opened",
            external_id="6",
            repository=repo,
            occurred_at=_t(0),
            issue_links=_snapshot(declares=[(repo, "1")]),
        ),
        _fact(
            event_type="change_request.updated",
            external_id="6",
            repository=repo,
            occurred_at=_t(0),  # same timestamp, DIFFERENT snapshot
            issue_links=_snapshot(declares=[(other_repo, "9")]),
        ),
        _fact(
            event_type="change_request.merged",
            external_id="6",
            repository=repo,
            occurred_at=_t(10),
        ),
        _fact(
            event_type="issue.closed",
            external_id="1",
            repository=repo,
            occurred_at=_t(20),
        ),
    ]

    projection = project_closure_episodes(facts)

    # both conflicting links are parked — never one arbitrarily won
    assert _link_states(projection, kind=ClosureLinkKind.DECLARES_CLOSURE) == {
        (repo, "1"): ClosureLinkState.PARKED.value,
        (other_repo, "9"): ClosureLinkState.PARKED.value,
    }
    episodes = _episode(projection, issue_repository=repo, issue_external_id="1")
    assert len(episodes) == 1
    assert episodes[0].status is ClosureEpisodeStatus.AMBIGUOUS
    assert episodes[0].change_request_external_id is None
    # parked unresolved is surfaced as a versioned unresolved record
    assert len(projection.unresolved) == 1
    assert projection.unresolved[0].reason == "ambiguous"


def test_webhook_outranks_backfill_at_equal_timestamps() -> None:
    """Differing snapshots at the same occurred_at from webhook vs backfill
    resolve to the webhook's content — no provider call, no park."""
    repo = "gitlab.com/cloudnative-pg"
    other_repo = "gitlab.com/other/project"
    facts = [
        _fact(
            event_type="change_request.opened",
            external_id="6",
            repository=repo,
            occurred_at=_t(0),
            observed_via="webhook",
            issue_links=_snapshot(declares=[(repo, "1")]),
        ),
        _fact(
            event_type="change_request.updated",
            external_id="6",
            repository=repo,
            occurred_at=_t(0),
            observed_via="backfill",
            issue_links=_snapshot(declares=[(other_repo, "9")]),
        ),
        _fact(
            event_type="change_request.merged",
            external_id="6",
            repository=repo,
            occurred_at=_t(10),
        ),
        _fact(
            event_type="issue.closed",
            external_id="1",
            repository=repo,
            occurred_at=_t(20),
        ),
    ]

    projection = project_closure_episodes(facts)

    # the webhook snapshot wins: #1 declared, #9 never seen
    assert _link_states(projection, kind=ClosureLinkKind.DECLARES_CLOSURE) == {
        (repo, "1"): ClosureLinkState.ACTIVE.value,
    }
    episodes = _episode(projection, issue_repository=repo, issue_external_id="1")
    assert episodes[0].status is ClosureEpisodeStatus.INFERRED
    assert episodes[0].change_request_external_id == "6"


# ── AC6: reopen/reclose episodes ─────────────────────────────────────────────


def test_reopen_reclose_produces_superseded_and_current_episodes() -> None:
    """A reopen/reclose cycle produces two immutable episodes: the earlier is
    superseded and the current projection points at the latest."""
    repo = "gitlab.com/cloudnative-pg"
    facts = [
        _fact(
            event_type="change_request.opened",
            external_id="6",
            repository=repo,
            occurred_at=_t(0),
            issue_links=_snapshot(declares=[(repo, "1")]),
        ),
        _fact(
            event_type="change_request.merged",
            external_id="6",
            repository=repo,
            occurred_at=_t(10),
        ),
        _fact(
            event_type="issue.closed",
            external_id="1",
            repository=repo,
            occurred_at=_t(20),
        ),
        _fact(
            event_type="issue.reopened",
            external_id="1",
            repository=repo,
            occurred_at=_t(30),
        ),
        _fact(
            event_type="issue.closed",
            external_id="1",
            repository=repo,
            occurred_at=_t(40),
        ),
    ]

    projection = project_closure_episodes(facts)

    episodes = _episode(projection, issue_repository=repo, issue_external_id="1")
    assert len(episodes) == 2
    first, latest = episodes  # chronological order: closed_at t20 then t40
    assert first.status is ClosureEpisodeStatus.SUPERSEDED
    assert first.closed_at == _t(20)
    # the superseded episode keeps its historical attribution
    assert first.change_request_external_id == "6"
    assert latest.status is ClosureEpisodeStatus.INFERRED
    assert latest.closed_at == _t(40)
    assert latest.change_request_external_id == "6"
    # only the latest close is unresolved-free; the earlier resolved cleanly too
    assert projection.unresolved == []


# ── open-interval statuses ───────────────────────────────────────────────────


def test_open_interval_with_unmerged_declaration_is_pending() -> None:
    """A declaration whose change request is not merged yields a pending open
    episode (no close observed)."""
    repo = "gitlab.com/cloudnative-pg"
    facts = [
        _fact(
            event_type="issue.opened",
            external_id="1",
            repository=repo,
            occurred_at=_t(0),
        ),
        _fact(
            event_type="change_request.opened",
            external_id="6",
            repository=repo,
            occurred_at=_t(5),
            issue_links=_snapshot(declares=[(repo, "1")]),
        ),
    ]

    projection = project_closure_episodes(facts)

    episodes = _episode(projection, issue_repository=repo, issue_external_id="1")
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode.status is ClosureEpisodeStatus.PENDING
    assert episode.closed_at is None
    assert episode.opened_at == _t(0)  # the issue's own open observation
    assert episode.change_request_external_id is None
    assert projection.unresolved == []


def test_open_interval_with_merged_declaration_is_awaiting_closure() -> None:
    """A merged change request with an active declaration and no observed close
    yields an awaiting_closure open episode."""
    repo = "gitlab.com/cloudnative-pg"
    facts = [
        _fact(
            event_type="change_request.opened",
            external_id="6",
            repository=repo,
            occurred_at=_t(0),
            issue_links=_snapshot(declares=[(repo, "1")]),
        ),
        _fact(
            event_type="change_request.merged",
            external_id="6",
            repository=repo,
            occurred_at=_t(10),
        ),
    ]

    projection = project_closure_episodes(facts)

    episodes = _episode(projection, issue_repository=repo, issue_external_id="1")
    assert len(episodes) == 1
    assert episodes[0].status is ClosureEpisodeStatus.AWAITING_CLOSURE
    assert episodes[0].closed_at is None


# ── references vs declares_closure are separate kinds ────────────────────────


def test_references_are_tracked_separately_and_never_create_episodes() -> None:
    """A plain reference (no closing syntax) records a references link and never
    fabricates a closure episode."""
    repo = "gitlab.com/cloudnative-pg"
    facts = [
        _fact(
            event_type="change_request.opened",
            external_id="6",
            repository=repo,
            occurred_at=_t(0),
            issue_links=_snapshot(references=[(repo, "1")]),
        ),
    ]

    projection = project_closure_episodes(facts)

    assert _link_states(projection, kind=ClosureLinkKind.REFERENCES) == {
        (repo, "1"): ClosureLinkState.ACTIVE.value
    }
    assert _link_states(projection, kind=ClosureLinkKind.DECLARES_CLOSURE) == {}
    # a reference alone never produces a closure episode
    assert _episode(projection, issue_repository=repo, issue_external_id="1") == []


# ── determinism ──────────────────────────────────────────────────────────────


def test_determinism_identical_input_any_order() -> None:
    """The projector is pure: identical input produces identical output
    regardless of input order, with stable sort order."""
    repo = "gitlab.com/cloudnative-pg"
    facts = [
        _fact(
            event_type="change_request.opened",
            external_id="6",
            repository=repo,
            occurred_at=_t(0),
            issue_links=_snapshot(declares=[(repo, "1"), (repo, "2")]),
        ),
        _fact(
            event_type="change_request.merged",
            external_id="6",
            repository=repo,
            occurred_at=_t(10),
        ),
        _fact(
            event_type="issue.closed",
            external_id="2",
            repository=repo,
            occurred_at=_t(20),
        ),
        _fact(
            event_type="issue.closed",
            external_id="1",
            repository=repo,
            occurred_at=_t(25),
        ),
    ]

    forward = project_closure_episodes(facts)
    backward = project_closure_episodes(list(reversed(facts)))

    assert forward.model_dump(mode="json") == backward.model_dump(mode="json")
    # episodes sorted by issue external id ("1" then "2")
    assert [ep.issue_external_id for ep in forward.episodes] == ["1", "2"]
