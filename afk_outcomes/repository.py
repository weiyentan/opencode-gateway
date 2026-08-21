"""Raw-asyncpg ``OutcomeRepository`` implementation (issue #448).

This is the only database-touching part of the ``afk_outcomes`` package.
It satisfies the :class:`afk_outcomes.interfaces.OutcomeRepository`
protocol (``save`` / ``get``) using asyncpg directly, consistent with the
Gateway's raw-asyncpg data-access convention, and deliberately imports
nothing from ``app`` (enforced mechanically by ``test_afk_outcomes_boundary``).

Write semantics
---------------

* **Engineering events are immutable facts** — inserted with
  ``ON CONFLICT DO NOTHING`` keyed on the event identity
  ``(provider, repository, entity_type, external_id, event_type,
  occurred_at)``.  Re-delivery no-ops.
* **``delivery_log`` is replay-safe** — written with
  ``ON CONFLICT (provider, delivery_id) DO NOTHING``.
* **State rows are enrich-only** — never hard-deleted, never silently
  confidence-lowered:

  * ``afk_runs`` — ``last_seen_at`` advanced; ``title``/``started_at``/
    ``finished_at``/``outcome`` COALESCE-filled (non-erasing); the derived
    ``outcome_status``/``status`` corrected toward the latest observation.
  * ``afk_run_entities`` — ``correlation_confidence`` raised with
    ``GREATEST`` (never lowered); ``evidence`` appended (never erased);
    ``last_seen_at`` advanced; a higher-confidence link marks weaker
    links for the same entity as ``superseded_at`` (never deleted).
  * ``unresolved_correlations`` — enrich-only, same raise/append rules.  Two
    kinds of row share the table: low-confidence ``Correlation`` links
    (entity+method keyed, ``reason`` NULL) and engine-emitted
    ambiguous/unmatched outcomes persisted via :meth:`save_unresolved`
    (run-level, ``afk_run`` sentinel entity, ``reason`` + ``candidates``).

Every derived link stores ``correlation_method``, ``correlation_confidence``,
``evidence``, and ``resolver_version``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Callable

import asyncpg

from afk_outcomes.closure_episodes import ClosureFact, project_closure_episodes
from afk_outcomes.interfaces import OutcomeRepository
from afk_outcomes.models import (
    AFKRun,
    CLOSURE_RESOLVER_VERSION,
    ClosureEpisode,
    ClosureEpisodeStatus,
    ClosureLink,
    ClosureProjection,
    ClosureUnresolved,
    Correlation,
    CorrelationEvidence,
    EngineeringEntity,
    EngineeringEvent,
    EngineeringOutcome,
    EntityType,
    ExecutionBinding,
    ExecutionOutcome,
    IssueLinkTarget,
    IssueLinksSnapshot,
    Provider,
    ReferenceSource,
    ResourceSessionAssociation,
    RunEntityLink,
    RunSessionLink,
    RunStatus,
    UnresolvedCorrelation,
    build_observation_key,
)

logger = logging.getLogger(__name__)

# Version of the correlation resolver that produces the derived links stored
# by this repository.  Bumped whenever link-derivation semantics change.
RESOLVER_VERSION = "2"

# Entity links with this role represent a definitive resolution; correlations
# for any other entity are treated as unresolved.
_RESOLVED_ROLE = "resolved"

# Sentinel ``entity_type`` used to key run-level engine unresolved outcomes
# (ambiguous/unmatched) in ``unresolved_correlations``.  These rows have no
# single engineering entity — the competing candidates live in ``candidates``
# — so ``external_id`` carries the run id and ``method`` mirrors ``reason``,
# making the existing UNIQUE(provider, repository, entity_type, external_id,
# method) a replay-safe (provider, repository, run, reason) identity.
_RUN_LEVEL_ENTITY_TYPE = "afk_run"


def _split_entity_id(entity_id: str) -> tuple[str, str]:
    """Split a domain ``entity_id`` (``"issue:437"``) into type and external id.

    The domain convention builds ``entity_id`` as ``"<entity_type>:<external_id>"``;
    this returns ``("issue", "437")``.  An id without a separator yields the
    whole string as both parts (defensive).
    """
    entity_type, sep, external_id = entity_id.partition(":")
    if not sep:
        return entity_type, entity_id
    return entity_type, external_id


def _provider_event_id(event: EngineeringEvent) -> str | None:
    """Return the provider's native event id when one is emitted.

    A provider that emits its own event id places it under
    ``payload["provider_event_id"]``; when present it is stored in the
    ``provider_event_id`` column and is the authority for ``occurred_at``.
    """
    payload = event.payload or {}
    value = payload.get("provider_event_id")
    return value if isinstance(value, str) and value else None


def _evidence_json(evidence: list[CorrelationEvidence]) -> str:
    """Serialize a list of :class:`CorrelationEvidence` to a JSONB-ready string."""
    return json.dumps([item.model_dump(mode="json") for item in evidence])


def _source_reference_json(sources: list[ReferenceSource]) -> str:
    """Serialize a list of :class:`ReferenceSource` to a JSONB-ready string."""
    return json.dumps([item.model_dump(mode="json") for item in sources])


def _row_to_execution_binding(row: asyncpg.Record) -> ExecutionBinding:
    """Convert an ``execution_bindings`` row to an :class:`ExecutionBinding`."""
    return ExecutionBinding(
        binding_id=str(row["id"]),
        awx_job={"job_id": str(row["awx_job_id"]), "job_template_id": 0},
        external_session_id=row["external_session_id"] or "",
        resource={
            "provider": row["provider"],
            "repository": row["repository_url"],
            "resource_type": row["entity_type"],
            "resource_number": row["entity_number"],
        },
        outcome=ExecutionOutcome(row["outcome"]) if row["outcome"] else ExecutionOutcome.COMPLETED,
        source_event_id=row["source_event_id"],
        branch=row["branch"],
        title=row["title"],
        failure_reason=row["failure_reason"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


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


class AsyncpgOutcomeRepository(OutcomeRepository):
    """Persist and retrieve AFK runs via a raw asyncpg connection.

    The connection is owned by the caller (acquired from a pool, or a mock
    in unit tests); this repository issues statements against it without
    managing transactions.
    """

    def __init__(
        self,
        conn: asyncpg.Connection,
        *,
        resolver_version: str = RESOLVER_VERSION,
    ) -> None:
        self._conn = conn
        self._resolver_version = resolver_version

    # ── write path ────────────────────────────────────────────────────

    async def save(self, run: AFKRun) -> None:
        """Persist ``run`` with enrich-only, replay-safe write semantics."""
        # The run row must exist before the delivery_log row is written:
        # ``delivery_log.afk_run_id`` carries a non-deferrable FK to
        # ``afk_runs.afk_run_id``, so the upsert runs first.
        await self._upsert_run(run)
        await self._log_delivery(run)

        entity_map: dict[str, EngineeringEntity] = {
            entity.entity_id: entity for entity in run.entities
        }
        correlation_map: dict[str, Correlation] = {
            correlation.entity_id: correlation for correlation in run.correlations
        }
        resolved_entity_ids: set[str] = {
            link.entity_id for link in run.entity_links if link.role == _RESOLVED_ROLE
        }

        for event in run.events:
            await self._insert_event(run, event, entity_map)

        for link in run.entity_links:
            await self._upsert_entity_link(run, link, entity_map, correlation_map)

        for link in run.session_links:
            await self._upsert_session_link(link)

        for correlation in run.correlations:
            if correlation.entity_id in resolved_entity_ids:
                continue  # resolved correlations enrich afk_run_entities, not here
            await self._upsert_unresolved_correlation(run, correlation, entity_map)

    async def save_associations(
        self, associations: list[ResourceSessionAssociation]
    ) -> None:
        """Persist exact resource<->session associations (issue #481).

        Each association is an explicit, deterministic link derived only from
        a stable resource reference carried in session metadata.  Writes are
        conflict-update — keyed on the resource identity + session identity
        ``UNIQUE (provider, repository, resource_type, resource_number,
        external_session_id)`` — so the same explicit reference converging on
        the same association never duplicates a row; on conflict
        ``last_seen_at`` is advanced with ``now()`` (recency tracking) and
        ``session_id`` is enriched via ``COALESCE`` so a previously-NULL
        session identity can be filled from a later observation.  There
        is no read-modify-write and therefore no advisory lock (a
        ``DO UPDATE SET`` is still a single atomic statement); the
        ``source_reference`` provenance is written once with the first insert
        and never re-merged.
        """
        for association in associations:
            await self._insert_association(association)

    async def _insert_association(
        self, association: ResourceSessionAssociation
    ) -> None:
        """Insert one association idempotently.

        On conflict (same resource+session identity) the row is never
        duplicated, but ``last_seen_at`` is advanced with ``now()`` and
        ``session_id`` is enriched via ``COALESCE`` so a previously-NULL
        session identity can be filled from a later observation.
        """
        await self._conn.execute(
            """
            INSERT INTO resource_session_associations
                (session_id, external_session_id, provider, repository,
                 resource_type, resource_number, source_reference,
                 resolver_version, first_seen_at, last_seen_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, now(), now())
            ON CONFLICT (provider, repository, resource_type, resource_number, external_session_id)
            DO UPDATE SET
                last_seen_at = now(),
                session_id = COALESCE(resource_session_associations.session_id, EXCLUDED.session_id)
            """,
            association.session_id,
            association.external_session_id,
            association.provider.value,
            association.repository,
            association.resource_type.value,
            association.resource_number,
            _source_reference_json(association.source_reference),
            association.resolver_version,
        )

    async def record_event(
        self,
        *,
        provider: Provider,
        delivery_id: str,
        entity: EngineeringEntity,
        event: EngineeringEvent,
    ) -> None:
        """Record one live provider delivery: a delivery_log row plus its event.

        Additive live-ingest seam (issue #451).  Unlike :meth:`save`, the
        owning ``afk_run_id`` is not known at live-ingest time (run identity
        is resolved later by the backfill/reconciliation engine), so the
        delivery is keyed on the provider's own delivery UUID
        (``X-GitHub-Delivery`` / ``X-GitLab-Event-UUID``, PRD decision #8)
        rather than the run id.  Both writes are conflict-ignore, so a
        redelivery of the same delivery UUID no-ops against the
        ``delivery_log`` UNIQUE(provider, delivery_id) and the
        ``engineering_events`` identity UNIQUE constraints.
        """
        await self._conn.execute(
            """
            INSERT INTO delivery_log (provider, delivery_id, afk_run_id, status, delivered_at)
            VALUES ($1, $2, $3, $4, now())
            ON CONFLICT (provider, delivery_id) DO NOTHING
            """,
            provider.value,
            delivery_id,
            None,
            event.event_type,
        )
        entity_type, external_id = _split_entity_id(event.entity_id)
        observation_key = event.observation_key or build_observation_key(
            provider=event.provider,
            repository=entity.repository,
            entity_type=entity_type,
            external_id=external_id,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
        )
        await self._conn.execute(
            """
            INSERT INTO engineering_events
                (provider, repository, entity_type, external_id, event_type,
                 occurred_at, provider_event_id, actor, payload, observation_key,
                 observed_via, snapshot_at, first_ingested_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, now())
            ON CONFLICT (provider, repository, entity_type, external_id, event_type, occurred_at)
            DO NOTHING
            """,
            event.provider.value,
            entity.repository,
            entity_type,
            external_id,
            event.event_type,
            event.occurred_at,
            _provider_event_id(event),
            event.actor,
            json.dumps(event.payload or {}),
            observation_key,
            event.observed_via,
            event.snapshot_at,
        )

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
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8,
                    CASE WHEN $8 = 'revoked' THEN now() ELSE NULL END,
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

    async def _log_delivery(self, run: AFKRun) -> None:
        """Record the delivery idempotently; re-delivery of the same run no-ops."""
        await self._conn.execute(
            """
            INSERT INTO delivery_log (provider, delivery_id, afk_run_id, status, delivered_at)
            VALUES ($1, $2, $3, $4, now())
            ON CONFLICT (provider, delivery_id) DO NOTHING
            """,
            run.provider.value,
            run.afk_run_id,
            run.afk_run_id,
            run.status.value,
        )

    async def _upsert_run(self, run: AFKRun) -> None:
        """Enrich-only upsert of the run aggregate (never erases, never deletes)."""
        outcome_status = run.outcome.status.value if run.outcome else None
        outcome_json = (
            json.dumps(run.outcome.model_dump(mode="json")) if run.outcome else None
        )
        await self._conn.execute(
            """
            INSERT INTO afk_runs
                (afk_run_id, provider, status, title, started_at, finished_at,
                 outcome_status, outcome, first_seen_at, last_seen_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now(), now())
            ON CONFLICT (afk_run_id) DO UPDATE SET
                status = EXCLUDED.status,
                title = COALESCE(EXCLUDED.title, afk_runs.title),
                started_at = COALESCE(afk_runs.started_at, EXCLUDED.started_at),
                finished_at = COALESCE(EXCLUDED.finished_at, afk_runs.finished_at),
                outcome_status = COALESCE(EXCLUDED.outcome_status, afk_runs.outcome_status),
                outcome = COALESCE(EXCLUDED.outcome, afk_runs.outcome),
                last_seen_at = now()
            """,
            run.afk_run_id,
            run.provider.value,
            run.status.value,
            run.title,
            run.started_at,
            run.finished_at,
            outcome_status,
            outcome_json,
        )

    async def _insert_event(
        self,
        run: AFKRun,
        event: EngineeringEvent,
        entity_map: dict[str, EngineeringEntity],
    ) -> None:
        """Insert an immutable engineering event (conflict-ignore)."""
        entity = entity_map.get(event.entity_id)
        entity_type, external_id = _split_entity_id(event.entity_id)
        repository = entity.repository if entity is not None else ""
        observation_key = event.observation_key or build_observation_key(
            provider=event.provider,
            repository=repository,
            entity_type=entity_type,
            external_id=external_id,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
        )
        await self._conn.execute(
            """
            INSERT INTO engineering_events
                (provider, repository, entity_type, external_id, event_type,
                 occurred_at, provider_event_id, actor, payload, observation_key,
                 observed_via, snapshot_at, first_ingested_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, now())
            ON CONFLICT (provider, repository, entity_type, external_id, event_type, occurred_at)
            DO NOTHING
            """,
            event.provider.value,
            repository,
            entity_type,
            external_id,
            event.event_type,
            event.occurred_at,
            _provider_event_id(event),
            event.actor,
            json.dumps(event.payload or {}),
            observation_key,
            event.observed_via,
            event.snapshot_at,
        )

    async def _upsert_entity_link(
        self,
        run: AFKRun,
        link: RunEntityLink,
        entity_map: dict[str, EngineeringEntity],
        correlation_map: dict[str, Correlation],
    ) -> None:
        """Enrich-only upsert of a derived entity link, then mark superseded peers.

        On conflict the ``superseded_at`` column is deliberately left out of
        the ``DO UPDATE`` set: a link that was once marked superseded stays
        superseded across re-deliveries.  Re-delivering the same entity
        mapping never re-activates it, preserving the one-authoritative-link
        guarantee relied on by ``get``'s ``superseded_at IS NULL`` filter.
        """
        entity = entity_map.get(link.entity_id)
        entity_type, external_id = _split_entity_id(link.entity_id)
        repository = entity.repository if entity is not None else ""
        provider = entity.provider.value if entity is not None else run.provider.value

        correlation = correlation_map.get(link.entity_id)
        correlation_method = correlation.method if correlation is not None else None
        evidence = correlation.evidence if correlation is not None else []
        owning_change_request_id = (
            entity.owning_change_request_id if entity is not None else None
        )

        await self._conn.execute(
            """
            INSERT INTO afk_run_entities
                (afk_run_id, provider, repository, entity_type, external_id,
                 owning_change_request_id, role, correlation_method, correlation_source,
                 correlation_confidence, evidence, resolver_version,
                 superseded_at, first_seen_at, last_seen_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, NULL, now(), now())
            ON CONFLICT (provider, repository, entity_type, external_id, afk_run_id)
            DO UPDATE SET
                correlation_confidence = GREATEST(
                    afk_run_entities.correlation_confidence, EXCLUDED.correlation_confidence
                ),
                evidence = afk_run_entities.evidence || EXCLUDED.evidence,
                correlation_method = COALESCE(
                    EXCLUDED.correlation_method, afk_run_entities.correlation_method
                ),
                resolver_version = COALESCE(
                    EXCLUDED.resolver_version, afk_run_entities.resolver_version
                ),
                role = EXCLUDED.role,
                correlation_source = EXCLUDED.correlation_source,
                owning_change_request_id = COALESCE(
                    EXCLUDED.owning_change_request_id,
                    afk_run_entities.owning_change_request_id
                ),
                last_seen_at = now()
            """,
            link.afk_run_id,
            provider,
            repository,
            entity_type,
            external_id,
            owning_change_request_id,
            link.role,
            correlation_method,
            link.correlation_source,
            link.correlation_confidence,
            _evidence_json(evidence),
            self._resolver_version,
        )

        # A higher-confidence link supersedes (marks, never deletes) weaker
        # links for the same entity owned by other runs.
        await self._conn.execute(
            """
            UPDATE afk_run_entities
            SET superseded_at = now()
            WHERE provider = $1
              AND repository = $2
              AND entity_type = $3
              AND external_id = $4
              AND afk_run_id <> $5
              AND superseded_at IS NULL
              AND correlation_confidence < $6
            """,
            provider,
            repository,
            entity_type,
            external_id,
            link.afk_run_id,
            link.correlation_confidence,
        )

    async def _upsert_session_link(self, link: RunSessionLink) -> None:
        """Enrich-only upsert of a session link (non-erasing, first/last seen)."""
        await self._conn.execute(
            """
            INSERT INTO afk_run_sessions
                (afk_run_id, session_id, external_session_id, started_at, finished_at,
                 first_seen_at, last_seen_at)
            VALUES ($1, $2, $3, $4, $5, now(), now())
            ON CONFLICT (afk_run_id, external_session_id) DO UPDATE SET
                session_id = COALESCE(EXCLUDED.session_id, afk_run_sessions.session_id),
                started_at = COALESCE(afk_run_sessions.started_at, EXCLUDED.started_at),
                finished_at = COALESCE(EXCLUDED.finished_at, afk_run_sessions.finished_at),
                last_seen_at = now()
            """,
            link.afk_run_id,
            link.session_id,
            link.external_session_id,
            link.started_at,
            link.finished_at,
        )

    async def _upsert_unresolved_correlation(
        self,
        run: AFKRun,
        correlation: Correlation,
        entity_map: dict[str, EngineeringEntity],
    ) -> None:
        """Enrich-only upsert of an unresolved correlation (raise/append only)."""
        entity = entity_map.get(correlation.entity_id)
        entity_type, external_id = _split_entity_id(correlation.entity_id)
        repository = entity.repository if entity is not None else ""
        provider = entity.provider.value if entity is not None else run.provider.value
        await self._conn.execute(
            """
            INSERT INTO unresolved_correlations
                (provider, repository, entity_type, external_id, afk_run_id, method,
                 correlation_confidence, evidence, resolver_version, created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now())
            ON CONFLICT (provider, repository, entity_type, external_id, afk_run_id, method)
            DO UPDATE SET
                correlation_confidence = GREATEST(
                    unresolved_correlations.correlation_confidence, EXCLUDED.correlation_confidence
                ),
                evidence = unresolved_correlations.evidence || EXCLUDED.evidence,
                resolver_version = COALESCE(
                    EXCLUDED.resolver_version, unresolved_correlations.resolver_version
                )
            """,
            provider,
            repository,
            entity_type,
            external_id,
            correlation.afk_run_id,
            correlation.method,
            correlation.correlation_confidence,
            _evidence_json(correlation.evidence),
            self._resolver_version,
        )

    async def save_unresolved(
        self,
        run: AFKRun,
        unresolved: list[UnresolvedCorrelation],
        *,
        repository: str,
    ) -> None:
        """Persist engine-emitted ambiguous/unmatched outcomes (enrich-only).

        The correlation engine surfaces genuinely unresolved outcomes as
        :class:`UnresolvedCorrelation` entries on ``ResolutionResult.unresolved``
        (issue #445); these carry no single engineering entity or correlation
        method — only ``reason`` (``ambiguous``/``unmatched``), competing
        ``candidates``, and ``evidence``.  Without this seam they existed only
        in the CLI report counters and were invisible to ``GET /correlations``.

        Each entry is upserted replay-safely: re-running the same window
        resolves the same run to the same id (deterministic ULID source), so
        the same ``(provider, repository, run, reason)`` identity re-converges
        via the enrich-only conflict update instead of duplicating rows.
        """
        for item in unresolved:
            await self._upsert_engine_unresolved(run, item, repository)

    async def _upsert_engine_unresolved(
        self,
        run: AFKRun,
        unresolved: UnresolvedCorrelation,
        repository: str,
    ) -> None:
        """Enrich-only upsert of one engine ambiguous/unmatched outcome.

        Run-level rows are keyed on a ``afk_run`` sentinel ``entity_type``
        with the run id as ``external_id`` and ``method`` mirroring ``reason``,
        so the table's ``UNIQUE (provider, repository, entity_type, external_id,
        afk_run_id, method)`` gives a replay-safe ``(provider, repository, run,
        reason)`` identity (``external_id == afk_run_id`` here).  ``reason``/
        ``candidates`` are COALESCE-filled (never erased) and ``evidence`` is
        appended, matching the enrich-only contract.
        """
        reason = unresolved.reason.value
        await self._conn.execute(
            """
            INSERT INTO unresolved_correlations
                (provider, repository, entity_type, external_id, afk_run_id, method,
                 reason, correlation_confidence, candidates, evidence, resolver_version,
                 created_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, now())
            ON CONFLICT (provider, repository, entity_type, external_id, afk_run_id, method)
            DO UPDATE SET
                reason = COALESCE(EXCLUDED.reason, unresolved_correlations.reason),
                candidates = COALESCE(EXCLUDED.candidates, unresolved_correlations.candidates),
                evidence = unresolved_correlations.evidence || EXCLUDED.evidence,
                resolver_version = COALESCE(
                    EXCLUDED.resolver_version, unresolved_correlations.resolver_version
                )
            """,
            run.provider.value,
            repository,
            _RUN_LEVEL_ENTITY_TYPE,
            unresolved.afk_run_id,
            unresolved.afk_run_id,
            reason,
            reason,
            0.0,
            json.dumps(unresolved.candidates),
            _evidence_json(unresolved.evidence),
            self._resolver_version,
        )

    # ── read path ─────────────────────────────────────────────────────

    async def get(self, afk_run_id: str) -> AFKRun | None:
        """Reconstruct an :class:`AFKRun` from the persisted tables (best effort).

        Returns ``None`` when no run with ``afk_run_id`` exists.  The read is
        lossy by design: engineering entities are reconstructed from the
        entity-link rows (descriptive metadata such as title/author are not
        persisted), and correlation ids are synthetic (not stored).
        """
        run_row = await self._conn.fetchrow(
            """
            SELECT afk_run_id, provider, status, title, started_at, finished_at,
                   outcome_status, outcome
            FROM afk_runs
            WHERE afk_run_id = $1
            """,
            afk_run_id,
        )
        if run_row is None:
            return None

        entity_rows = await self._conn.fetch(
            """
            SELECT provider, repository, entity_type, external_id,
                   owning_change_request_id, role, correlation_method,
                   correlation_source, correlation_confidence, evidence
            FROM afk_run_entities
            WHERE afk_run_id = $1 AND superseded_at IS NULL
            """,
            afk_run_id,
        )
        session_rows = await self._conn.fetch(
            """
            SELECT session_id, external_session_id, started_at, finished_at
            FROM afk_run_sessions
            WHERE afk_run_id = $1
            """,
            afk_run_id,
        )
        event_rows = await self._conn.fetch(
            """
            SELECT provider, repository, entity_type, external_id, event_type,
                   occurred_at, provider_event_id, actor, payload
            FROM engineering_events
            WHERE (provider, repository, entity_type, external_id) IN (
                SELECT provider, repository, entity_type, external_id FROM afk_run_entities
                WHERE afk_run_id = $1 AND superseded_at IS NULL
            )
            """,
            afk_run_id,
        )

        entities: list[EngineeringEntity] = []
        entity_links: list[RunEntityLink] = []
        correlations: list[Correlation] = []
        seen_entities: set[tuple[str, str, str, str]] = set()

        for row in entity_rows:
            entity_id = f"{row['entity_type']}:{row['external_id']}"
            entity_key = (row["provider"], row["repository"], row["entity_type"], row["external_id"])
            if entity_key not in seen_entities:
                seen_entities.add(entity_key)
                entities.append(
                    EngineeringEntity(
                        entity_id=entity_id,
                        entity_type=EntityType(row["entity_type"]),
                        provider=Provider(row["provider"]),
                        repository=row["repository"],
                        owning_change_request_id=row["owning_change_request_id"],
                    )
                )
            entity_links.append(
                RunEntityLink(
                    afk_run_id=afk_run_id,
                    entity_id=entity_id,
                    role=row["role"],
                    correlation_confidence=row["correlation_confidence"],
                    correlation_source=row["correlation_source"],
                )
            )
            if row["correlation_method"] is not None:
                correlations.append(
                    Correlation(
                        correlation_id=f"{afk_run_id}:{entity_id}",
                        afk_run_id=afk_run_id,
                        entity_id=entity_id,
                        correlation_confidence=row["correlation_confidence"],
                        method=row["correlation_method"],
                        evidence=[
                            CorrelationEvidence.model_validate(item)
                            for item in (row["evidence"] or [])
                        ],
                    )
                )

        events: list[EngineeringEvent] = []
        for row in event_rows:
            entity_id = f"{row['entity_type']}:{row['external_id']}"
            events.append(
                EngineeringEvent(
                    event_id=f"{entity_id}:{row['event_type']}",
                    event_type=row["event_type"],
                    provider=Provider(row["provider"]),
                    entity_id=entity_id,
                    occurred_at=row["occurred_at"],
                    actor=row["actor"],
                    payload=row["payload"] or {},
                )
            )

        session_links: list[RunSessionLink] = [
            RunSessionLink(
                afk_run_id=afk_run_id,
                session_id=str(row["session_id"]) if row["session_id"] else None,
                external_session_id=row["external_session_id"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
            )
            for row in session_rows
        ]

        outcome = None
        if run_row["outcome"] is not None:
            outcome = EngineeringOutcome.model_validate(run_row["outcome"])

        return AFKRun(
            afk_run_id=run_row["afk_run_id"],
            provider=Provider(run_row["provider"]),
            status=RunStatus(run_row["status"]),
            title=run_row["title"],
            started_at=run_row["started_at"],
            finished_at=run_row["finished_at"],
            entities=entities,
            events=events,
            correlations=correlations,
            outcome=outcome,
            entity_links=entity_links,
            session_links=session_links,
        )

    # ── execution bindings ────────────────────────────────────────────

    async def save_execution_binding(self, binding: ExecutionBinding) -> None:
        """Persist one execution binding idempotently (issue #547).

        **Idempotent by AWX job identity**: ``awx_job_id`` is the unique
        key.  Repeating the same binding (identical ``awx_job_id``) is a
        successful no-op — the INSERT ON CONFLICT DO NOTHING ensures no
        duplicate row is created and no existing row is overwritten.

        **Conflict rejection**: When a binding with the same ``awx_job_id``
        already exists and the incoming data differs, the ON CONFLICT DO
        NOTHING clause silently ignores the conflicting insert rather than
        overwriting the original record.  The caller may detect this by
        checking if a row already exists before inserting (see
        :meth:`get_execution_binding_by_awx_job_id`).

        **Multiple jobs per resource**: Different AWX jobs targeting the same
        GitHub pull request or GitLab merge request (same provider resource
        identity) are both persisted — the provider resource columns are NOT
        part of any unique constraint.
        """
        await self._conn.execute(
            """
            INSERT INTO execution_bindings
                (awx_job_id, external_session_id, provider, repository_url,
                 entity_type, entity_number, outcome, source_event_id,
                 branch, title, failure_reason, started_at, finished_at,
                 created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                    now(), now())
            ON CONFLICT (awx_job_id) DO NOTHING
            """,
            int(binding.awx_job.job_id),
            binding.external_session_id,
            binding.resource.provider.value,
            binding.resource.repository,
            binding.resource.resource_type.value,
            binding.resource.resource_number,
            binding.outcome.value,
            binding.source_event_id,
            binding.branch,
            binding.title,
            binding.failure_reason,
            binding.started_at,
            binding.finished_at,
        )

    async def get_execution_binding_by_awx_job_id(
        self, awx_job_id: str
    ) -> ExecutionBinding | None:
        """Return one execution binding by AWX job ID, or ``None`` (issue #547).

        The AWX job ID is the idempotency key; at most one row exists for a
        given job id.  Returns ``None`` when no binding with that job id
        exists.
        """
        row = await self._conn.fetchrow(
            """
            SELECT id, awx_job_id, external_session_id, provider, repository_url,
                   entity_type, entity_number, outcome, source_event_id,
                   branch, title, failure_reason, started_at, finished_at
            FROM execution_bindings
            WHERE awx_job_id = $1
            """,
            int(awx_job_id),
        )
        if row is None:
            return None
        return _row_to_execution_binding(row)

    async def list_execution_bindings_for_resource(
        self,
        *,
        provider: Provider,
        repository: str,
        resource_type: EntityType,
        resource_number: str,
    ) -> list[ExecutionBinding]:
        """Return all execution bindings for a provider resource (issue #547).

        Ordered deterministically by ``created_at ASC`` (earliest first,
        failed-then-successful retry visible in history).  Different AWX
        jobs targeting the same GitHub pull request or GitLab merge request
        are both returned.
        """
        rows = await self._conn.fetch(
            """
            SELECT id, awx_job_id, external_session_id, provider, repository_url,
                   entity_type, entity_number, outcome, source_event_id,
                   branch, title, failure_reason, started_at, finished_at
            FROM execution_bindings
            WHERE provider = $1
              AND repository_url = $2
              AND entity_type = $3
              AND entity_number = $4
            ORDER BY created_at ASC
            """,
            provider.value,
            repository,
            resource_type.value,
            resource_number,
        )
        return [_row_to_execution_binding(row) for row in rows]
