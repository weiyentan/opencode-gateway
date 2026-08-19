"""Closure-episode projection from immutable engineering facts (pure domain).

Derives the change-request->issue closure relationship from the immutable
``engineering_events`` facts (Slice 2) into a versioned, rebuildable
closure-episode projection plus versioned unresolved records — never a
separate source of truth, never authoritative over facts.  Style precedent:
:mod:`afk_outcomes.associations` — deterministic, pure, provider-agnostic,
imports nothing from ``app``.

Rules (PRD #521, issue #524):

* **Facts project in provider ``occurred_at`` order**, never ingestion
  order — out-of-order deliveries converge on the same projection.
* **Snapshot diffs**: an ``issue_links`` snapshot is a full snapshot of the
  change request's current ``references`` and ``declares_closure`` sets.  A
  link present in one snapshot and absent in the next is explicitly
  revoked; a later re-appearance re-activates it.  Facts without an
  ``issue_links`` field do not participate in diffs (a missing field is
  never a revocation).
* **Ordering policy D**: within one ``occurred_at``, identical snapshots
  coalesce (harmless); differing snapshots resolve to the webhook's
  content when exactly one distinct webhook snapshot exists (webhook
  outranks backfill at equal timestamps, no provider call); all other
  conflicts park — the conflicting links are marked ``parked`` and never
  arbitrarily won.
* **Episodes**: an open->close interval per issue.  A closed episode is
  ``inferred`` only when exactly one eligible change request has an active
  declaration whose merge **strictly precedes** the issue-close
  observation; zero eligible candidates yield ``unmatched``; multiple — or
  any eligible parked declaration — yield ``ambiguous``.  Unmatched and
  ambiguous outcomes are emitted as versioned
  :class:`~afk_outcomes.models.ClosureUnresolved` records — never
  tie-broken, never scored, never a heuristic winner.
* **Open intervals**: an active declaration whose change request is not
  merged renders ``pending``; once a declaring change request has merged it
  renders ``awaiting_closure``.  A reopened interval with no declaration
  yet renders ``pending``.
* **Reopen/reclose**: every observed close produces a new immutable
  episode keyed by (issue, closed_at); all earlier episodes are
  ``superseded`` and the latest episode is the current projection.
* **References and declarations are separate kinds**: a plain reference
  never produces a closure episode.

Determinism: identical input produces identical output regardless of input
order — stable sorts, order-independent snapshot comparison, no randomness,
no clock dependence.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from afk_outcomes.models import (
    CLOSURE_RESOLVER_VERSION,
    ClosureCandidate,
    ClosureEpisode,
    ClosureEpisodeStatus,
    ClosureLink,
    ClosureLinkKind,
    ClosureLinkState,
    ClosureProjection,
    ClosureUnresolved,
    EntityType,
    IssueLinksSnapshot,
    Provider,
)
from pydantic import BaseModel, Field

# ── key shapes ───────────────────────────────────────────────────────────────

_CRKey = tuple[str, str, str]  # (provider.value, repository, external_id)
_IssueKey = tuple[str, str, str]  # (provider.value, repository, external_id)
_LinkKey = tuple[str, str, str]  # (kind, issue_repository, issue_external_id)

_ISSUE_EVENT_TYPES = frozenset({"issue.opened", "issue.reopened", "issue.closed"})
_SNAPSHOT_EVENT_TYPES = frozenset(
    {"change_request.opened", "change_request.updated"}
)
_MERGE_EVENT_TYPE = "change_request.merged"


class ClosureFact(BaseModel):
    """One normalized fact projected from ``engineering_events``.

    Repositories are already-normalized identities: the caller normalizes
    ``issue_links`` repository URLs before constructing facts (the pure
    domain package never imports the application's URL normalizer).
    """

    provider: Provider
    repository: str
    entity_type: EntityType
    external_id: str
    event_type: str
    occurred_at: datetime
    observed_via: str = Field(description="'webhook' or 'backfill'")
    issue_links: IssueLinksSnapshot | None = Field(
        default=None,
        description="Full issue_links snapshot (change-request open/update only)",
    )


# ── snapshot canonicalization + same-timestamp resolution ────────────────────


def _snapshot_key(
    snapshot: IssueLinksSnapshot,
) -> frozenset[tuple[str, str, str]]:
    """Order-independent canonical form of one snapshot (link tuples)."""
    return frozenset(
        (kind, target.repository, target.number)
        for kind, targets in (
            (ClosureLinkKind.DECLARES_CLOSURE.value, snapshot.declares_closure),
            (ClosureLinkKind.REFERENCES.value, snapshot.references),
        )
        for target in targets
    )


def _resolve_timestamp_group(
    snapshot_facts: Sequence[ClosureFact],
) -> tuple[str, frozenset[tuple[str, str, str]] | list[frozenset[tuple[str, str, str]]]]:
    """Resolve same-``occurred_at`` snapshots under ordering policy D.

    Returns ``("ok", canonical_snapshot)`` when the group resolves to a
    single effective snapshot (all identical, or webhook outranking
    backfill at equal timestamps), or ``("park", [conflicting_snapshots])``
    when differing same-source snapshots conflict and must park unresolved —
    never arbitrarily won.
    """
    webhook = {
        _snapshot_key(fact.issue_links)  # type: ignore[arg-type]
        for fact in snapshot_facts
        if fact.observed_via == "webhook"
    }
    backfill = {
        _snapshot_key(fact.issue_links)  # type: ignore[arg-type]
        for fact in snapshot_facts
        if fact.observed_via != "webhook"
    }
    distinct = webhook | backfill
    if len(distinct) == 1:
        # identical snapshots (any source mix) coalesce — harmless
        return ("ok", next(iter(distinct)))
    if len(webhook) == 1:
        # webhook outranks backfill at equal timestamps (no provider call)
        return ("ok", next(iter(webhook)))
    return ("park", sorted(distinct, key=lambda snap: sorted(snap)))


# ── per-change-request link states ────────────────────────────────────────────


def _derive_link_states(facts: Sequence[ClosureFact]) -> dict[_LinkKey, str]:
    """Derive the per-link states of one change request from its snapshots.

    Groups snapshot facts by ``occurred_at``, resolves each timestamp group
    under ordering policy D, then applies full-snapshot diffs: a link in the
    effective snapshot is active; a previously active/parked link absent
    from it is revoked; in a parked group the links all conflicting views
    agree on are active, the differing links park, and links absent from
    every conflicting view are revoked (unanimous absence).
    """
    groups: dict[datetime, list[ClosureFact]] = {}
    for fact in facts:
        if fact.event_type in _SNAPSHOT_EVENT_TYPES and fact.issue_links is not None:
            groups.setdefault(fact.occurred_at, []).append(fact)

    states: dict[_LinkKey, str] = {}
    for occurred_at in sorted(groups):
        resolution = _resolve_timestamp_group(groups[occurred_at])
        if resolution[0] == "ok":
            current = resolution[1]  # type: ignore[assignment]
            for link in current:
                states[link] = ClosureLinkState.ACTIVE.value
            for link in list(states):
                if (
                    states[link]
                    in (ClosureLinkState.ACTIVE.value, ClosureLinkState.PARKED.value)
                    and link not in current
                ):
                    states[link] = ClosureLinkState.REVOKED.value
            continue

        # park branch: never arbitrarily win a conflicting same-timestamp pair
        candidates: list[frozenset[tuple[str, str, str]]] = resolution[1]  # type: ignore[assignment]
        common: frozenset[tuple[str, str, str]] = frozenset.intersection(*candidates) if candidates else frozenset()
        union: frozenset[tuple[str, str, str]] = frozenset.union(*candidates) if candidates else frozenset()
        for link in common:
            states[link] = ClosureLinkState.ACTIVE.value
        for link in union - common:
            states[link] = ClosureLinkState.PARKED.value
        for link in list(states):
            if (
                link not in union
                and states[link] != ClosureLinkState.REVOKED.value
            ):
                states[link] = ClosureLinkState.REVOKED.value
    return states


# ── per-issue episode reconstruction ──────────────────────────────────────────


def _closed_episode(
    *,
    issue_provider: Provider,
    issue_repository: str,
    issue_external_id: str,
    opened_at: datetime | None,
    closed_at: datetime,
    active_crs: Sequence[_CRKey],
    parked_crs: Sequence[_CRKey],
    cr_merged: dict[_CRKey, datetime],
    resolver_version: str,
) -> tuple[ClosureEpisode, ClosureUnresolved | None]:
    """Compute one closed episode and its optional unresolved record.

    Eligible candidates are active-declaration change requests whose merge
    strictly precedes the close observation; eligible parked declarations
    make the outcome indeterminate (ambiguous) regardless of the active
    count.  Exactly one eligible candidate is ``inferred``; zero is
    ``unmatched``; anything else is ``ambiguous``.  Never tie-broken.
    """
    eligible_active = sorted(
        cr
        for cr in active_crs
        if (merged_at := cr_merged.get(cr)) is not None and merged_at < closed_at
    )
    eligible_parked = sorted(
        cr
        for cr in parked_crs
        if (merged_at := cr_merged.get(cr)) is not None and merged_at < closed_at
    )
    candidates = sorted(set(eligible_active) | set(eligible_parked))

    if eligible_parked:
        status = ClosureEpisodeStatus.AMBIGUOUS
    elif len(eligible_active) == 1:
        status = ClosureEpisodeStatus.INFERRED
    elif not eligible_active:
        status = ClosureEpisodeStatus.UNMATCHED
    else:
        status = ClosureEpisodeStatus.AMBIGUOUS

    attributed = eligible_active[0] if status is ClosureEpisodeStatus.INFERRED else None
    episode = ClosureEpisode(
        issue_provider=issue_provider,
        issue_repository=issue_repository,
        issue_external_id=issue_external_id,
        opened_at=opened_at,
        closed_at=closed_at,
        status=status,
        change_request_provider=Provider(attributed[0]) if attributed else None,
        change_request_repository=attributed[1] if attributed else None,
        change_request_external_id=attributed[2] if attributed else None,
        resolver_version=resolver_version,
    )

    unresolved: ClosureUnresolved | None = None
    if status is ClosureEpisodeStatus.UNMATCHED:
        unresolved = ClosureUnresolved(
            issue_provider=issue_provider,
            issue_repository=issue_repository,
            issue_external_id=issue_external_id,
            closed_at=closed_at,
            reason="unmatched",
            candidates=[],
            resolver_version=resolver_version,
        )
    elif status is ClosureEpisodeStatus.AMBIGUOUS:
        unresolved = ClosureUnresolved(
            issue_provider=issue_provider,
            issue_repository=issue_repository,
            issue_external_id=issue_external_id,
            closed_at=closed_at,
            reason="ambiguous",
            candidates=[
                ClosureCandidate(
                    provider=Provider(cr[0]),
                    repository=cr[1],
                    external_id=cr[2],
                )
                for cr in candidates
            ],
            resolver_version=resolver_version,
        )
    return episode, unresolved


def _open_episode(
    *,
    issue_provider: Provider,
    issue_repository: str,
    issue_external_id: str,
    opened_at: datetime | None,
    active_crs: Sequence[_CRKey],
    cr_merged: dict[_CRKey, datetime],
    resolver_version: str,
) -> ClosureEpisode:
    """Compute the currently-open interval's episode.

    ``awaiting_closure`` when any declaring change request has merged;
    otherwise ``pending`` (including a reopened interval with no
    declaration yet).  An open episode never attributes a change request.
    """
    status = (
        ClosureEpisodeStatus.AWAITING_CLOSURE
        if any(cr in cr_merged for cr in active_crs)
        else ClosureEpisodeStatus.PENDING
    )
    return ClosureEpisode(
        issue_provider=issue_provider,
        issue_repository=issue_repository,
        issue_external_id=issue_external_id,
        opened_at=opened_at,
        closed_at=None,
        status=status,
        resolver_version=resolver_version,
    )


def _project_issue_episodes(
    issue_key: _IssueKey,
    events: Sequence[tuple[datetime, str]],
    active_crs: Sequence[_CRKey],
    parked_crs: Sequence[_CRKey],
    cr_merged: dict[_CRKey, datetime],
    resolver_version: str,
) -> list[tuple[ClosureEpisode, ClosureUnresolved | None]]:
    """Reconstruct the ordered immutable episode list for one issue.

    Every observed close produces a closed episode; a currently-open
    interval (no close after the last open/reopen) materializes an open
    episode when the issue has an active declaration or prior closed
    episodes (a reopen cycle).  All episodes except the latest are marked
    ``superseded``.  Each closed episode carries its optional unresolved
    record (unmatched/ambiguous), whose candidates mirror the episode's
    own eligible-candidate computation — never an inconsistent superset.
    """
    provider = Provider(issue_key[0])
    episodes: list[tuple[ClosureEpisode, ClosureUnresolved | None]] = []
    open_start: datetime | None = None
    for occurred_at, event_type in sorted(events, key=lambda item: (item[0], item[1])):
        if event_type == "issue.opened":
            if open_start is None:
                open_start = occurred_at
        elif event_type == "issue.reopened":
            open_start = occurred_at
        else:  # issue.closed
            episode, unresolved = _closed_episode(
                issue_provider=provider,
                issue_repository=issue_key[1],
                issue_external_id=issue_key[2],
                opened_at=open_start,
                closed_at=occurred_at,
                active_crs=active_crs,
                parked_crs=parked_crs,
                cr_merged=cr_merged,
                resolver_version=resolver_version,
            )
            episodes.append((episode, unresolved))
            open_start = None
    if open_start is not None and (active_crs or episodes):
        episodes.append(
            (
                _open_episode(
                    issue_provider=provider,
                    issue_repository=issue_key[1],
                    issue_external_id=issue_key[2],
                    opened_at=open_start,
                    active_crs=active_crs,
                    cr_merged=cr_merged,
                    resolver_version=resolver_version,
                ),
                None,
            )
        )
    elif not events and active_crs:
        # an issue with no observed lifecycle events of its own, but with an
        # active declaration — materialize the open interval (opened_at unknown)
        episodes.append(
            (
                _open_episode(
                    issue_provider=provider,
                    issue_repository=issue_key[1],
                    issue_external_id=issue_key[2],
                    opened_at=None,
                    active_crs=active_crs,
                    cr_merged=cr_merged,
                    resolver_version=resolver_version,
                ),
                None,
            )
        )
    for episode, _ in episodes[:-1]:
        episode.status = ClosureEpisodeStatus.SUPERSEDED
    return episodes


# ── projector ─────────────────────────────────────────────────────────────────


def project_closure_episodes(
    facts: Sequence[ClosureFact],
    *,
    issues: frozenset[_IssueKey] | None = None,
    resolver_version: str = CLOSURE_RESOLVER_VERSION,
) -> ClosureProjection:
    """Project closure episodes + link states + unresolved records from facts.

    Computes, for every change request present in ``facts``, its per-kind
    link states (snapshot diffs in ``occurred_at`` order, ordering policy D)
    and, for every issue present in the facts' issue events or linked by a
    declaration, its immutable episode list (all but the latest
    superseded) plus versioned unresolved records.

    ``issues`` optionally restricts episode/unresolved computation to the
    given issue identities (the repository passes the affected set of an
    incremental recompute); link states are always computed for every
    change request in ``facts`` because a change request's snapshot history
    is loaded completely.

    The output is deterministic: identical input produces identical output
    regardless of input order.
    """
    cr_facts: dict[_CRKey, list[ClosureFact]] = {}
    cr_merged: dict[_CRKey, datetime] = {}
    issue_events: dict[_IssueKey, list[tuple[datetime, str]]] = {}

    for fact in facts:
        if fact.entity_type is EntityType.ISSUE:
            if fact.event_type in _ISSUE_EVENT_TYPES:
                key: _IssueKey = (fact.provider.value, fact.repository, fact.external_id)
                issue_events.setdefault(key, []).append((fact.occurred_at, fact.event_type))
            continue
        if fact.entity_type is EntityType.CHANGE_REQUEST:
            key: _CRKey = (fact.provider.value, fact.repository, fact.external_id)
            cr_facts.setdefault(key, []).append(fact)
            if fact.event_type == _MERGE_EVENT_TYPE:
                previous = cr_merged.get(key)
                if previous is None or fact.occurred_at < previous:
                    cr_merged[key] = fact.occurred_at

    # ── per-change-request link states ──
    link_states: dict[_CRKey, dict[_LinkKey, str]] = {
        cr_key: _derive_link_states(cr_fact_list)
        for cr_key, cr_fact_list in cr_facts.items()
    }

    # ── active/parked declaring change requests per issue ──
    active_declaring: dict[_IssueKey, list[_CRKey]] = {}
    parked_declaring: dict[_IssueKey, list[_CRKey]] = {}
    for cr_key, states in link_states.items():
        for (kind, issue_repository, issue_external_id), state in states.items():
            if kind != ClosureLinkKind.DECLARES_CLOSURE.value:
                continue
            if state == ClosureLinkState.REVOKED.value:
                continue  # a revoked declaration is not a candidate at all
            issue_key: _IssueKey = (cr_key[0], issue_repository, issue_external_id)
            bucket = (
                active_declaring
                if state == ClosureLinkState.ACTIVE.value
                else parked_declaring
            )
            bucket.setdefault(issue_key, []).append(cr_key)

    all_issue_keys = set(issue_events) | set(active_declaring) | set(parked_declaring)
    if issues is not None:
        all_issue_keys &= issues

    # ── episodes + unresolved records ──
    episodes: list[ClosureEpisode] = []
    unresolved: list[ClosureUnresolved] = []
    for issue_key in sorted(all_issue_keys):
        active_crs = sorted(set(active_declaring.get(issue_key, ())))
        parked_crs = sorted(set(parked_declaring.get(issue_key, ())))
        for episode, record in _project_issue_episodes(
            issue_key,
            issue_events.get(issue_key, ()),
            active_crs,
            parked_crs,
            cr_merged,
            resolver_version,
        ):
            episodes.append(episode)
            if record is not None:
                unresolved.append(record)

    # ── links ──
    links: list[ClosureLink] = []
    for cr_key in sorted(link_states):
        for link_key in sorted(link_states[cr_key]):
            kind, issue_repository, issue_external_id = link_key
            links.append(
                ClosureLink(
                    change_request_provider=Provider(cr_key[0]),
                    change_request_repository=cr_key[1],
                    change_request_external_id=cr_key[2],
                    issue_provider=Provider(cr_key[0]),
                    issue_repository=issue_repository,
                    issue_external_id=issue_external_id,
                    kind=ClosureLinkKind(kind),
                    state=ClosureLinkState(link_states[cr_key][link_key]),
                    resolver_version=resolver_version,
                )
            )

    return ClosureProjection(
        links=links,
        episodes=episodes,
        unresolved=unresolved,
        resolver_version=resolver_version,
    )
