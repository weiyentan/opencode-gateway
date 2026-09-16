"""Closure-projection seam of the ``AsyncpgOutcomeRepository`` (issue #683).

Split out of the former monolithic ``afk_outcomes/repository.py``: the
closure-episode projection recompute (``recompute_closure_projection``) and
operator rebuild (``rebuild_closure_projection``), the fact/payload decoding
helpers, and the rebuild result dataclass.  The methods are composed onto the
facade class ``AsyncpgOutcomeRepository`` (see the package ``__init__``) and
keep their exact pre-split signatures.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from afk_outcomes.closure_episodes import ClosureFact, project_closure_episodes
from afk_outcomes.models import (
    CLOSURE_RESOLVER_VERSION,
    ClosureEpisode,
    ClosureEpisodeStatus,
    ClosureLink,
    ClosureProjection,
    ClosureUnresolved,
    EngineeringEntity,
    EngineeringEvent,
    EntityType,
    IssueLinkTarget,
    IssueLinksSnapshot,
    Provider,
)
from afk_outcomes.repository.crud import _split_entity_id

logger = logging.getLogger(__name__)


def _decode_jsonb(raw: object) -> dict | None:
    """Decode a JSONB value that asyncpg may return as a dict or a JSON string.

    asyncpg returns JSONB columns as JSON strings unless a codec is
    registered, so the repository boundary must tolerate both shapes.  Returns
    the decoded object when it is a JSON object (``dict``), or ``None`` when
    the value is missing, malformed JSON, or a non-object payload.  ``None``
    is never a valid JSONB object, so callers treat it as "no usable object"
    and omit closure metadata while preserving the committed fact.  Diagnostics
    are bounded — a fixed message, never the raw payload contents.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("Closure projection: malformed JSONB payload (skipped)")
            return None
        if isinstance(decoded, dict):
            return decoded
        logger.warning("Closure projection: non-object JSONB payload (skipped)")
        return None
    if raw is None:
        return None
    logger.warning(
        "Closure projection: unexpected JSONB payload type %s (skipped)",
        type(raw).__name__,
    )
    return None


def _issue_links_from_payload(
    raw: object,
    normalize: Callable[[str], str | None],
) -> IssueLinksSnapshot | None:
    """Extract a normalized :class:`IssueLinksSnapshot` from a fact payload.

    The producer stores ``issue_links`` repository URLs verbatim; the caller
    supplies the application's URL normalizer so link targets resolve to the
    same normalized identities as ``engineering_events.repository``.  Targets
    whose repository cannot be normalized are skipped (never an identity
    collision); a payload without an ``issue_links`` dict yields ``None``
    (a missing field is never a revocation — see the projector).

    The ``issue_links`` value may arrive as a dict or a JSON string (asyncpg
    JSONB shape); both are decoded.  Malformed individual link entries are
    skipped while valid entries in the same payload are retained, and
    ``references`` / ``declares_closure`` stay in distinct buckets.
    """
    decoded = _decode_jsonb(raw)
    if decoded is None:
        return None
    snapshot = IssueLinksSnapshot()
    found = False
    for field, kind in (("references", "references"), ("declares_closure", "declares_closure")):
        items = decoded.get(field)
        if not isinstance(items, list):
            continue
        targets: list[IssueLinkTarget] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            repository = item.get("repository")
            number = item.get("number")
            if not isinstance(repository, str) or not isinstance(number, str):
                continue
            normalized = normalize(repository)
            if normalized is None:
                continue
            targets.append(IssueLinkTarget(repository=normalized, number=number))
        if kind == "references":
            snapshot.references = targets
        else:
            snapshot.declares_closure = targets
        found = found or bool(items)
    return snapshot if found else None


def _to_closure_fact(
    *,
    provider: Provider,
    repository: str,
    entity_type: EntityType,
    external_id: str,
    event_type: str,
    occurred_at: object,
    observed_via: object,
    payload: object,
    normalize: Callable[[str], str | None],
) -> ClosureFact:
    """Build a :class:`ClosureFact` from an ``engineering_events``-shaped row.

    ``payload`` may be a dict or a JSON string (asyncpg JSONB shape).  A
    malformed or non-object payload never crashes the fact build: the fact is
    still produced (the committed fact is preserved) with closure metadata
    omitted.
    """
    decoded = _decode_jsonb(payload)
    issue_links_raw = decoded.get("issue_links") if decoded is not None else None
    return ClosureFact(
        provider=provider,
        repository=repository,
        entity_type=entity_type,
        external_id=external_id,
        event_type=event_type,
        occurred_at=occurred_at,
        observed_via=observed_via,
        issue_links=_issue_links_from_payload(issue_links_raw, normalize),
    )


def _closure_fact_issue_keys(fact: ClosureFact) -> list[tuple[str, str, str]]:
    """Return the issue keys one closure fact touches (empty when it touches none).

    An issue fact touches its own issue identity
    ``(provider.value, repository, external_id)``; a change-request fact
    touches every issue in its ``issue_links`` snapshot — both
    ``declares_closure`` and ``references`` targets — as
    ``(provider.value, target.repository, target.number)``.  Used by the
    windowed rebuild to decide which issues have their ENTIRE fact history
    inside the requested window.
    """
    if fact.entity_type is EntityType.ISSUE:
        return [(fact.provider.value, fact.repository, fact.external_id)]
    if fact.issue_links is None:
        return []
    return [
        (fact.provider.value, target.repository, target.number)
        for target in fact.issue_links.declares_closure + fact.issue_links.references
    ]


#: Fact event types the closure-episode projection consumes.  Anything else
#: (issue.updated, change_request.closed/reopened, …) carries no closure-
#: relevant signal and never triggers a recompute.
_CLOSURE_RELEVANT_EVENT_TYPES = frozenset(
    {
        "issue.opened",
        "issue.reopened",
        "issue.closed",
        "change_request.opened",
        "change_request.updated",
        "change_request.merged",
    }
)


@dataclass
class ClosureRebuildResult:
    """The outcome of a full closure-projection rebuild (issue #539).

    Carries the recomputed :class:`ClosureProjection` plus the processed
    fact range so the operator CLI can report what was rebuilt.
    """

    projection: ClosureProjection
    facts_processed: int
    event_range_start: datetime | None
    event_range_end: datetime | None


class _ClosureProjectionRepositoryMixin:
    'Closure-projection methods of :class:`AsyncpgOutcomeRepository`.'

    async def recompute_closure_projection(
        self,
        *,
        seed_event: EngineeringEvent,
        seed_entity: EngineeringEntity,
        normalize_repository: Callable[[str], str | None] | None = None,
    ) -> None:
        """Recompute the closure-episode projection for one committed fact (issue #524).

        DB-local, event-triggered recompute: the caller invokes this AFTER the
        facts transaction committed (write boundary — facts first, projection
        second, best-effort).  The affected issues are the seed fact's own
        issue (issue lifecycle facts) or, for a change-request fact, every
        issue in its ``issue_links`` snapshot plus every issue already linked
        to that change request in ``closure_links`` (so a snapshot-diff
        revocation and a merge both reach their episodes).

        The recompute loads the complete fact history of the affected issues
        and of every change request linked to them (revoked links are
        retained, so the declaring set is complete), projects it with the
        pure-domain projector scoped to the affected issues, and reconciles
        the derived state into ``closure_links`` / ``closure_episodes`` /
        ``closure_unresolved``.  Every write is a deterministic upsert of
        recomputed state, so a partial failure or a concurrent recompute
        converges on the next trigger — the projection is rebuildable from
        facts and never authoritative over them.

        ``normalize_repository`` converts raw producer repository URLs inside
        ``issue_links`` snapshots to the same normalized identities the facts
        carry (the pure-domain package cannot import the application
        normalizer, so the caller supplies it).  Defaults to identity.

        The caller owns transaction boundaries: statements run in asyncpg
        autocommit when invoked outside a transaction.  A failure here must
        never block ingestion — the caller wraps this best-effort.
        """
        normalize = (
            normalize_repository
            if normalize_repository is not None
            else (lambda value: value)
        )
        if seed_event.event_type not in _CLOSURE_RELEVANT_EVENT_TYPES:
            return
        _, external_id = _split_entity_id(seed_event.entity_id)
        seed_fact = _to_closure_fact(
            provider=seed_event.provider,
            repository=seed_entity.repository,
            entity_type=seed_entity.entity_type,
            external_id=external_id,
            event_type=seed_event.event_type,
            occurred_at=seed_event.occurred_at,
            observed_via=seed_event.observed_via,
            payload=seed_event.payload or {},
            normalize=normalize,
        )

        # ── affected issue identities ──────────────────────────────────
        affected: set[tuple[str, str, str]] = set()
        if seed_fact.entity_type is EntityType.ISSUE:
            affected.add(
                (seed_fact.provider.value, seed_fact.repository, seed_fact.external_id)
            )
        else:  # change_request
            if seed_fact.issue_links is not None:
                for target in (
                    seed_fact.issue_links.declares_closure
                    + seed_fact.issue_links.references
                ):
                    affected.add((seed_fact.provider.value, target.repository, target.number))
            linked = await self._conn.fetch(
                """
                SELECT DISTINCT issue_provider, issue_repository, issue_external_id
                FROM closure_links
                WHERE change_request_provider = $1
                  AND change_request_repository = $2
                  AND change_request_external_id = $3
                """,
                seed_fact.provider.value,
                seed_fact.repository,
                seed_fact.external_id,
            )
            for row in linked:
                affected.add(
                    (row["issue_provider"], row["issue_repository"], row["issue_external_id"])
                )
        if not affected:
            return
        affected_sorted = sorted(affected)
        issue_providers = [key[0] for key in affected_sorted]
        issue_repositories = [key[1] for key in affected_sorted]
        issue_external_ids = [key[2] for key in affected_sorted]

        # ── load the affected issues' lifecycle facts ──────────────────
        issue_rows = await self._conn.fetch(
            """
            SELECT provider, repository, entity_type, external_id, event_type,
                   occurred_at, observed_via, payload
            FROM engineering_events
            WHERE entity_type = 'issue'
              AND (provider, repository, external_id) IN (
                  SELECT * FROM unnest($1::text[], $2::text[], $3::text[]))
            """,
            issue_providers,
            issue_repositories,
            issue_external_ids,
        )

        # ── every change request linked to the affected issues ─────────
        cr_link_rows = await self._conn.fetch(
            """
            SELECT DISTINCT change_request_provider,
                            change_request_repository,
                            change_request_external_id
            FROM closure_links
            WHERE (issue_provider, issue_repository, issue_external_id) IN (
                  SELECT * FROM unnest($1::text[], $2::text[], $3::text[]))
            """,
            issue_providers,
            issue_repositories,
            issue_external_ids,
        )
        cr_keys: set[tuple[str, str, str]] = {
            (
                row["change_request_provider"],
                row["change_request_repository"],
                row["change_request_external_id"],
            )
            for row in cr_link_rows
        }
        if seed_fact.entity_type is EntityType.CHANGE_REQUEST:
            cr_keys.add(
                (seed_fact.provider.value, seed_fact.repository, seed_fact.external_id)
            )
        cr_keys_sorted = sorted(cr_keys)
        cr_rows = await self._conn.fetch(
            """
            SELECT provider, repository, entity_type, external_id, event_type,
                   occurred_at, observed_via, payload
            FROM engineering_events
            WHERE entity_type = 'change_request'
              AND (provider, repository, external_id) IN (
                  SELECT * FROM unnest($1::text[], $2::text[], $3::text[]))
            """,
            [key[0] for key in cr_keys_sorted],
            [key[1] for key in cr_keys_sorted],
            [key[2] for key in cr_keys_sorted],
        )

        facts: list[ClosureFact] = []
        for row in issue_rows:
            facts.append(
                _to_closure_fact(
                    provider=Provider(row["provider"]),
                    repository=row["repository"],
                    entity_type=EntityType(row["entity_type"]),
                    external_id=row["external_id"],
                    event_type=row["event_type"],
                    occurred_at=row["occurred_at"],
                    observed_via=row["observed_via"],
                    payload=row["payload"] or {},
                    normalize=normalize,
                )
            )
        for row in cr_rows:
            facts.append(
                _to_closure_fact(
                    provider=Provider(row["provider"]),
                    repository=row["repository"],
                    entity_type=EntityType(row["entity_type"]),
                    external_id=row["external_id"],
                    event_type=row["event_type"],
                    occurred_at=row["occurred_at"],
                    observed_via=row["observed_via"],
                    payload=row["payload"] or {},
                    normalize=normalize,
                )
            )

        projection = project_closure_episodes(
            facts,
            issues=frozenset(affected),
            resolver_version=CLOSURE_RESOLVER_VERSION,
        )

        # ── reconcile (deterministic upserts — rebuildable from facts) ─
        for link in projection.links:
            await self._upsert_closure_link(link)
        await self._reconcile_closure_episodes(projection.episodes)
        for record in projection.unresolved:
            await self._upsert_closure_unresolved(record)

    async def rebuild_closure_projection(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        normalize_repository: Callable[[str], str | None] | None = None,
    ) -> ClosureRebuildResult:
        """Rebuild the closure-episode projection from committed facts (issue #539).

        Operator-only rebuild operation (CLI/AWX): reads every closure-relevant
        ``engineering_events`` fact, projects it with the same pure-domain
        projector (:func:`afk_outcomes.closure_episodes.project_closure_episodes`)
        and reconciles the derived state into ``closure_links`` /
        ``closure_episodes`` / ``closure_unresolved`` via the same reconcile
        helpers as the incremental recompute.

        **Full rebuild** (no bounds): every closure-relevant fact is projected
        with no issue restriction and, in addition to the upsert reconcile,
        stored ``closure_links`` and ``closure_unresolved`` rows absent from
        the fresh projection are deleted — repeated full rebuilds converge to
        identical projection state.

        **Windowed rebuild** (``since``/``until`` given): a windowed rebuild
        must never persist projection state derived from an incomplete fact
        history.  Only issues whose ENTIRE closure-relevant fact history is
        fully contained within ``[since, until]`` are written ("whole-window"
        issues): any issue with a touching fact outside the window is
        excluded from the write set entirely, so a bounded rebuild can never
        regress an already-correct episode (e.g. overwrite a
        ``CLOSED``/``SUPERSEDED`` episode with ``AWAITING_CLOSURE``).  The
        projector's ``issues`` restriction scopes episodes/unresolved to the
        whole-window issues, and the always-computed link states are filtered
        to the same set before writing.  Non-whole-window issues are left
        untouched — never written, never deleted.

        ``normalize_repository`` converts raw producer repository URLs inside
        ``issue_links`` snapshots to the same normalized identities the facts
        carry (the pure-domain package cannot import the application
        normalizer, so the caller supplies it).  Defaults to identity.

        The caller owns transaction boundaries.  Returns a
        :class:`ClosureRebuildResult` carrying the recomputed projection and
        the processed fact range for reporting.
        """
        normalize = (
            normalize_repository
            if normalize_repository is not None
            else (lambda value: value)
        )
        rows = await self._conn.fetch(
            """
            SELECT provider, repository, entity_type, external_id, event_type,
                   occurred_at, observed_via, payload
            FROM engineering_events
            WHERE event_type = ANY($1::text[])
            """,
            list(_CLOSURE_RELEVANT_EVENT_TYPES),
        )

        all_facts: list[ClosureFact] = [
            _to_closure_fact(
                provider=Provider(row["provider"]),
                repository=row["repository"],
                entity_type=EntityType(row["entity_type"]),
                external_id=row["external_id"],
                event_type=row["event_type"],
                occurred_at=row["occurred_at"],
                observed_via=row["observed_via"],
                payload=row["payload"] or {},
                normalize=normalize,
            )
            for row in rows
        ]

        windowed = since is not None or until is not None
        issues_restriction: frozenset[tuple[str, str, str]] | None = None
        if windowed:
            # A bounded rebuild must not persist state derived from an
            # incomplete fact history.  Compute, over the COMPLETE fact set,
            # every issue's touching-fact times; an issue is whole-window only
            # when ALL of them fall inside [since, until].
            issue_fact_times: dict[tuple[str, str, str], list[datetime]] = {}
            for fact in all_facts:
                for issue_key in _closure_fact_issue_keys(fact):
                    issue_fact_times.setdefault(issue_key, []).append(
                        fact.occurred_at
                    )
            issues_restriction = frozenset(
                issue_key
                for issue_key, times in issue_fact_times.items()
                if all(
                    (since is None or occurred_at >= since)
                    and (until is None or occurred_at <= until)
                    for occurred_at in times
                )
            )

        facts: list[ClosureFact] = []
        range_start: datetime | None = None
        range_end: datetime | None = None
        for fact in all_facts:
            if since is not None and fact.occurred_at < since:
                continue
            if until is not None and fact.occurred_at > until:
                continue
            facts.append(fact)
            if range_start is None or fact.occurred_at < range_start:
                range_start = fact.occurred_at
            if range_end is None or fact.occurred_at > range_end:
                range_end = fact.occurred_at

        projection = project_closure_episodes(
            facts,
            issues=issues_restriction,
            resolver_version=CLOSURE_RESOLVER_VERSION,
        )
        if issues_restriction is not None:
            # link states are computed for every change request in ``facts``
            # regardless of the ``issues`` restriction — drop links whose
            # issue is not whole-window so a bounded rebuild never writes
            # them (and never regresses their stored state).
            projection.links = [
                link
                for link in projection.links
                if (
                    link.issue_provider.value,
                    link.issue_repository,
                    link.issue_external_id,
                )
                in issues_restriction
            ]

        # ── reconcile (deterministic upserts — rebuildable from facts) ─
        for link in projection.links:
            await self._upsert_closure_link(link)
        await self._reconcile_closure_episodes(projection.episodes)
        for record in projection.unresolved:
            await self._upsert_closure_unresolved(record)

        # A FULL rebuild additionally removes projection rows the fresh
        # projection no longer produces, so repeated full rebuilds converge
        # to identical projection state.  A windowed rebuild never deletes.
        if not windowed:
            await self._reconcile_closure_links_absent(projection)
            await self._reconcile_closure_unresolved_absent(projection)

        return ClosureRebuildResult(
            projection=projection,
            facts_processed=len(facts),
            event_range_start=range_start,
            event_range_end=range_end,
        )

    async def _upsert_closure_link(self, link: ClosureLink) -> None:
        """Upsert one derived link state, corrected toward the latest derivation.

        The projection is a recomputed view over facts, not an enrich-only
        log: ``state`` (active/revoked/parked) is corrected on conflict, and
        ``revoked_at`` is stamped only while the link is revoked (cleared on
        re-activation).  Deterministic recompute makes the upsert idempotent.
        """
        await self._conn.execute(
            """
            INSERT INTO closure_links
                (change_request_provider, change_request_repository,
                 change_request_external_id, issue_provider, issue_repository,
                 issue_external_id, kind, state, revoked_at, resolver_version,
                 first_seen_at, last_seen_at, derived_at)
             VALUES ($1, $2, $3, $4, $5, $6, $7, $8::varchar,
                    CASE WHEN $8::varchar = 'revoked' THEN now() ELSE NULL END,
                    $9, now(), now(), now())
            ON CONFLICT (change_request_provider, change_request_repository,
                         change_request_external_id, issue_provider,
                         issue_repository, issue_external_id, kind)
            DO UPDATE SET
                state = EXCLUDED.state,
                revoked_at = CASE WHEN EXCLUDED.state = 'revoked' THEN now() ELSE NULL END,
                resolver_version = EXCLUDED.resolver_version,
                derived_at = now(),
                last_seen_at = now()
            """,
            link.change_request_provider.value,
            link.change_request_repository,
            link.change_request_external_id,
            link.issue_provider.value,
            link.issue_repository,
            link.issue_external_id,
            link.kind.value,
            link.state.value,
            link.resolver_version,
        )

    async def _reconcile_closure_episodes(
        self, episodes: list[ClosureEpisode]
    ) -> None:
        """Reconcile the computed episode list into ``closure_episodes``.

        Matches computed episodes to stored rows by (issue identity,
        closed_at) — the open episode against the stored current open row —
        updates matched rows toward the recomputed state (the current
        episode's ``superseded_at`` cleared, superseded episodes' stamp
        preserved), inserts new rows, and marks stored rows the projector no
        longer produces as superseded (never deleted).  The partial unique
        index (one current episode per issue) guarantees the current pointer.
        """
        by_issue: dict[tuple[str, str, str], list[ClosureEpisode]] = {}
        for episode in episodes:
            key = (
                episode.issue_provider.value,
                episode.issue_repository,
                episode.issue_external_id,
            )
            by_issue.setdefault(key, []).append(episode)
        if not by_issue:
            return

        providers = [key[0] for key in sorted(by_issue)]
        repositories = [key[1] for key in sorted(by_issue)]
        external_ids = [key[2] for key in sorted(by_issue)]
        stored_rows = await self._conn.fetch(
            """
            SELECT id, issue_provider, issue_repository, issue_external_id,
                   closed_at, superseded_at
            FROM closure_episodes
            WHERE (issue_provider, issue_repository, issue_external_id) IN (
                  SELECT * FROM unnest($1::text[], $2::text[], $3::text[]))
            """,
            providers,
            repositories,
            external_ids,
        )
        stored_by_issue: dict[tuple[str, str, str], list] = {}
        for row in stored_rows:
            key = (row["issue_provider"], row["issue_repository"], row["issue_external_id"])
            stored_by_issue.setdefault(key, []).append(row)

        for issue_key in sorted(by_issue):
            computed = by_issue[issue_key]
            stored = stored_by_issue.get(issue_key, [])
            matched_ids: set = set()
            stored_closed = {
                row["closed_at"]: row
                for row in stored
                if row["closed_at"] is not None
            }
            stored_open_current = next(
                (
                    row
                    for row in stored
                    if row["closed_at"] is None and row["superseded_at"] is None
                ),
                None,
            )
            for index, episode in enumerate(computed):
                is_current = index == len(computed) - 1
                row = (
                    stored_open_current
                    if episode.closed_at is None
                    else stored_closed.get(episode.closed_at)
                )
                if row is not None:
                    matched_ids.add(row["id"])
                    await self._conn.execute(
                        """
                        UPDATE closure_episodes
                        SET opened_at = $2,
                            closed_at = $3,
                            status = $4,
                            change_request_provider = $5,
                            change_request_repository = $6,
                            change_request_external_id = $7,
                            resolver_version = $8,
                            superseded_at = CASE WHEN $9 THEN
                                COALESCE(closure_episodes.superseded_at, now())
                                ELSE NULL END,
                            derived_at = now(),
                            last_seen_at = now()
                        WHERE id = $1
                        """,
                        row["id"],
                        episode.opened_at,
                        episode.closed_at,
                        episode.status.value,
                        (
                            episode.change_request_provider.value
                            if episode.change_request_provider is not None
                            else None
                        ),
                        episode.change_request_repository,
                        episode.change_request_external_id,
                        episode.resolver_version,
                        not is_current,
                    )
                else:
                    await self._conn.execute(
                        """
                        INSERT INTO closure_episodes
                            (issue_provider, issue_repository, issue_external_id,
                             opened_at, closed_at, status,
                             change_request_provider, change_request_repository,
                             change_request_external_id, resolver_version,
                             superseded_at, derived_at, first_seen_at, last_seen_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                                CASE WHEN $11 THEN NULL ELSE now() END,
                                now(), now(), now())
                        """,
                        episode.issue_provider.value,
                        episode.issue_repository,
                        episode.issue_external_id,
                        episode.opened_at,
                        episode.closed_at,
                        episode.status.value,
                        (
                            episode.change_request_provider.value
                            if episode.change_request_provider is not None
                            else None
                        ),
                        episode.change_request_repository,
                        episode.change_request_external_id,
                        episode.resolver_version,
                        is_current,
                    )
            # stored rows the projector no longer produces (e.g. an open
            # interval whose declarations were all revoked) are superseded —
            # never deleted, never re-activated.
            for row in stored:
                if row["id"] in matched_ids:
                    continue
                if row["superseded_at"] is not None:
                    continue
                await self._conn.execute(
                    """
                    UPDATE closure_episodes
                    SET status = $2,
                        superseded_at = now(),
                        derived_at = now(),
                        last_seen_at = now()
                    WHERE id = $1
                    """,
                    row["id"],
                    ClosureEpisodeStatus.SUPERSEDED.value,
                )

    async def _upsert_closure_unresolved(self, record: ClosureUnresolved) -> None:
        """Upsert one versioned unresolved record (enrich-corrected, never deleted).

        Keyed by (issue identity, closed_at, reason) — one record per
        unresolved episode outcome, versioned via ``resolver_version`` and
        ``derived_at``.  Historical records of episodes that later resolved
        are retained (no hard delete anywhere in the projection).
        """
        await self._conn.execute(
            """
            INSERT INTO closure_unresolved
                (issue_provider, issue_repository, issue_external_id,
                 closed_at, reason, candidates, resolver_version,
                 derived_at, first_seen_at, last_seen_at)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, now(), now(), now())
            ON CONFLICT (issue_provider, issue_repository, issue_external_id,
                         closed_at, reason)
            DO UPDATE SET
                candidates = EXCLUDED.candidates,
                resolver_version = EXCLUDED.resolver_version,
                derived_at = now(),
                last_seen_at = now()
            """,
            record.issue_provider.value,
            record.issue_repository,
            record.issue_external_id,
            record.closed_at,
            record.reason,
            json.dumps([item.model_dump(mode="json") for item in record.candidates]),
            record.resolver_version,
        )

    async def _reconcile_closure_links_absent(
        self, projection: ClosureProjection
    ) -> None:
        """Delete stored ``closure_links`` rows absent from the fresh projection.

        Full-rebuild convergence seam (issue #539 review fix): the reconcile
        loop only ever upserts (and the incremental recompute deliberately
        never deletes), so a full rebuild additionally removes link rows the
        fresh projection no longer produces — repeated full rebuilds converge
        to identical projection state.  The link key is the seven-column row
        identity ``(change-request tuple, issue tuple, kind)``.  Windowed
        rebuilds never call this: they must not delete anything.
        """
        present = {
            (
                link.change_request_provider.value,
                link.change_request_repository,
                link.change_request_external_id,
                link.issue_provider.value,
                link.issue_repository,
                link.issue_external_id,
                link.kind.value,
            )
            for link in projection.links
        }
        existing_rows = await self._conn.fetch(
            """
            SELECT change_request_provider, change_request_repository,
                   change_request_external_id, issue_provider, issue_repository,
                   issue_external_id, kind
            FROM closure_links
            """
        )
        stale = {
            (
                row["change_request_provider"],
                row["change_request_repository"],
                row["change_request_external_id"],
                row["issue_provider"],
                row["issue_repository"],
                row["issue_external_id"],
                row["kind"],
            )
            for row in existing_rows
        } - present
        for key in sorted(stale):
            await self._conn.execute(
                """
                DELETE FROM closure_links
                WHERE change_request_provider = $1
                  AND change_request_repository = $2
                  AND change_request_external_id = $3
                  AND issue_provider = $4
                  AND issue_repository = $5
                  AND issue_external_id = $6
                  AND kind = $7
                """,
                *key,
            )

    async def _reconcile_closure_unresolved_absent(
        self, projection: ClosureProjection
    ) -> None:
        """Delete stored ``closure_unresolved`` rows absent from the fresh projection.

        Full-rebuild convergence seam, mirroring
        :meth:`_reconcile_closure_links_absent`: historical unresolved rows
        are normally retained (the incremental recompute never deletes), but
        a full rebuild removes rows the fresh projection no longer produces,
        keyed by ``(issue tuple, closed_at, reason)``.  Windowed rebuilds
        never call this: they must not delete anything.
        """
        present = {
            (
                record.issue_provider.value,
                record.issue_repository,
                record.issue_external_id,
                record.closed_at,
                record.reason,
            )
            for record in projection.unresolved
        }
        existing_rows = await self._conn.fetch(
            """
            SELECT issue_provider, issue_repository, issue_external_id,
                   closed_at, reason
            FROM closure_unresolved
            """
        )
        stale = {
            (
                row["issue_provider"],
                row["issue_repository"],
                row["issue_external_id"],
                row["closed_at"],
                row["reason"],
            )
            for row in existing_rows
        } - present
        for key in sorted(stale):
            await self._conn.execute(
                """
                DELETE FROM closure_unresolved
                WHERE issue_provider = $1
                  AND issue_repository = $2
                  AND issue_external_id = $3
                  AND closed_at = $4
                  AND reason = $5
                """,
                *key,
            )
