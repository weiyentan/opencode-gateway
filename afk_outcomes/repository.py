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
from afk_outcomes.run_status import resolve_afk_run_status
from afk_outcomes.serialization import ULIDSource
from afk_outcomes.models import (
    AFKRun,
    AFKRunLifecycle,
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
    TriggerType,
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


def _parse_awx_job_id(awx_job_id: str) -> int:
    """Coerce an AWX job id string to int, rejecting non-numeric values.

    The API layer validates the id before reaching the repository; this
    guard keeps direct repository callers from surfacing a bare
    ``ValueError`` from ``int()`` (issue #549 review).  The raised error
    carries a clear message instead of leaking the raw conversion failure.
    """
    try:
        return int(awx_job_id)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid AWX job id: {awx_job_id!r}") from exc


def _row_to_execution_binding(row: asyncpg.Record) -> ExecutionBinding:
    """Convert an ``execution_bindings`` row to an :class:`ExecutionBinding`.

    Columns added in later migrations (``afk_run_id``, ``trigger_type``) are
    read with ``.get()`` so legacy rows missing them default to ``None``.
    The provider resource identity (issue #590) reads back as ``None`` when
    the row carries no change-request identity (failed/cancelled executions
    persist without one).
    """
    resource = None
    if (
        row["provider"] is not None
        and row["repository_url"] is not None
        and row["entity_type"] is not None
        and row["entity_number"] is not None
    ):
        resource = {
            "provider": row["provider"],
            "repository": row["repository_url"],
            "resource_type": row["entity_type"],
            "resource_number": row["entity_number"],
        }
    # Normalized session attribution (issue #627): the additive JSONB column
    # (migration 0042) carries the full deduplicated collection when present;
    # legacy rows without the column fall back to the singular nullable
    # column, normalized to a one-element collection.  A binding with no
    # resolved session reads back an empty collection.
    raw_session_ids = row.get("external_session_ids_json")
    if isinstance(raw_session_ids, str):
        try:
            decoded = json.loads(raw_session_ids)
        except ValueError:
            decoded = None
    else:
        decoded = raw_session_ids
    if isinstance(decoded, list):
        session_ids = [s for s in decoded if isinstance(s, str) and s]
        # Deduplicate preserving first-occurrence order.
        session_ids = list(dict.fromkeys(session_ids))
    else:
        singular = row["external_session_id"]
        session_ids = [singular] if singular else []
    primary_session = session_ids[0] if session_ids else None
    return ExecutionBinding(
        binding_id=str(row["id"]),
        awx_job={
            "job_id": str(row["awx_job_id"]),
            "job_template_id": row["job_template_id"],
        },
        external_session_id=primary_session,
        external_session_ids=session_ids,
        resource=resource,
        outcome=ExecutionOutcome(row["outcome"]) if row["outcome"] else ExecutionOutcome.COMPLETED,
        source_event_id=row["source_event_id"],
        branch=row["branch"],
        title=row["title"],
        failure_reason=row["failure_reason"],
        failure_summary=row.get("failure_summary"),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        afk_run_id=row.get("afk_run_id"),
        trigger_type=row.get("trigger_type"),
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


@dataclass(frozen=True)
class CreateAFKExecutionBindingResult:
    """Result of a transactional AFK run + execution binding creation (issue #584).

    Returned by :meth:`AsyncpgOutcomeRepository.create_or_replay_afk_execution_binding`.
    Exactly one of ``is_conflict``, ``is_created``, ``is_reused``, or
    ``run_missing`` is ``True``; idempotent replays set none of the four
    (all ``False``).

    ``is_reused`` (PR #600 blocker) signals that a *new* execution binding
    was inserted attached to an *existing* lifecycle: the canonical
    change-request identity already owned an ``afk_runs`` row, so this
    execution adopted that winner instead of provisioning a second
    lifecycle.

    ``run_missing`` (issue #595) signals that the caller supplied an
    ``afk_run_id`` referencing no provisioned lifecycle — nothing was
    inserted.
    """

    afk_run_id: str
    binding_id: int | None = None
    is_conflict: bool = False
    is_created: bool = False
    run_missing: bool = False
    is_reused: bool = False


@dataclass(frozen=True)
class ProvisionAFKRunResult:
    """Result of a provisional lifecycle provisioning attempt (issue #589).

    Returned by :meth:`AsyncpgOutcomeRepository.provision_afk_run`:

    * ``is_created=True`` — a genuinely-new lifecycle row was inserted.
    * ``is_conflict=True`` — the provisioning key
      ``(provider, host, source_event_id)`` already exists with a
      different payload; nothing was mutated.
    * ``predecessor_missing=True`` — ``recovered_from_afk_run_id``
      references a run that does not exist; nothing was inserted.
    * Idempotent replay sets none of the three flags — the existing row is
      returned unchanged.
    """

    afk_run_id: str
    is_created: bool = False
    is_conflict: bool = False
    predecessor_missing: bool = False


@dataclass(frozen=True)
class UpdateExecutionBindingResult:
    """Result of a terminal-update attempt (issue #590).

    Returned by :meth:`AsyncpgOutcomeRepository.update_execution_binding_terminal`:

    * ``is_updated=True`` — the stored ``running`` row was transitioned to
      the requested terminal outcome (with non-erasing fill-ins).
    * ``is_conflict=True`` — the stored row is already terminal with a
      different payload, or a supplied fill-in contradicts a stored
      non-null value; nothing was mutated (history is never overwritten).
    * ``not_found=True`` — no binding exists for ``awx_job_id``.
    * Idempotent replay (already terminal, identical payload) sets none of
      the three flags — the stored row is returned unchanged.
    """

    binding_id: int | None = None
    is_updated: bool = False
    is_conflict: bool = False
    not_found: bool = False


@dataclass(frozen=True)
class ChangeRequestBindingResult:
    """Result of an explicit change-request binding attempt (issue #589).

    Returned by :meth:`AsyncpgOutcomeRepository.bind_change_request`:

    * ``is_bound=True`` — the lifecycle's change request was newly set.
    * ``is_conflict=True`` — the lifecycle already carries a different
      change request, or the requested change request already belongs to
      another lifecycle (the 1:1 invariant); nothing was mutated.
    * ``run_missing=True`` — no run with ``afk_run_id`` exists.
    * Idempotent replay (same identity already bound) sets none of the
      three flags.
    """

    afk_run_id: str
    is_bound: bool = False
    is_conflict: bool = False
    run_missing: bool = False


@dataclass(frozen=True)
class ChangeRequestLookupResult:
    """Result of a change-request -> owning run lookup (issue #597).

    Returned by :meth:`AsyncpgOutcomeRepository.get_afk_run_by_change_request`:

    * ``afk_run_id`` — the owning lifecycle's ULID when exactly one run is
      bound to the change request.
    * ``is_conflict=True`` — more than one lifecycle claims the change
      request (an impossible ownership conflict — the 1:1 invariant was
      violated); nothing is chosen arbitrarily.
    * ``afk_run_id=None`` and ``is_conflict=False`` — no lifecycle is bound
      to the change request (unknown or unbound).
    """

    afk_run_id: str | None = None
    is_conflict: bool = False


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
                    CASE WHEN $8::text = 'revoked' THEN now() ELSE NULL END,
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

    async def _resolve_internal_session_id(self, external_session_id: str) -> str | None:
        """Resolve an external session id to the internal ``sessions.id`` UUID.

        Best-effort enrichment for execution-binding session links (issue
        #618): multiple internal sessions may share one external session id
        (they are scoped by ``source_database_id``, which is not available at
        this call site).  When exactly one Gateway session matches, its
        ``sessions.id`` is returned.  When zero match, ``None`` is returned.
        When 2+ match, ``None`` is returned as a fail-safe — the caller never
        guesses between competing internal sessions, and the link is
        persisted with the ``external_session_id`` only and ``session_id``
        NULL.
        """
        rows = await self._conn.fetch(
            """
            SELECT id FROM sessions
            WHERE external_session_id = $1
            LIMIT 2
            """,
            external_session_id,
        )
        if len(rows) == 1:
            return str(rows[0]["id"])
        return None

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
            raw_payload = row["payload"] or {}
            if isinstance(raw_payload, str):
                # asyncpg returns JSONB as str unless a codec is registered.
                raw_payload = json.loads(raw_payload)
            events.append(
                EngineeringEvent(
                    event_id=f"{entity_id}:{row['event_type']}",
                    event_type=row["event_type"],
                    provider=Provider(row["provider"]),
                    entity_id=entity_id,
                    occurred_at=row["occurred_at"],
                    actor=row["actor"],
                    payload=raw_payload,
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

    async def save_execution_binding(self, binding: ExecutionBinding) -> int | None:
        """Persist one execution binding atomically (issue #568).

        The INSERT is the linearisation point: ``UNIQUE (awx_job_id)`` is
        enforced by the database, and ``ON CONFLICT DO NOTHING RETURNING id``
        lets the caller distinguish a genuinely-new insert from a
        conflict-skip in a single round-trip.

        * **Returns the inserted row's ``id``** when the insert succeeded
          (the caller should respond with ``201 Created``).
        * **Returns ``None``** when the insert was skipped due to a
          ``UNIQUE`` conflict — another concurrent or earlier insert won the
          race.  The caller must then fetch the existing row and compare
          fields to decide between ``200`` (idempotent replay) and
          ``409`` (conflicting data).

        **Multiple jobs per resource**: Different AWX jobs targeting the same
        GitHub pull request or GitLab merge request (same provider resource
        identity) are both persisted — the provider resource columns are NOT
        part of any unique constraint.
        """
        rows = await self._conn.fetch(
            """
            INSERT INTO execution_bindings
                (awx_job_id, job_template_id, external_session_id, provider,
                 repository_url, entity_type, entity_number, outcome,
                 source_event_id, branch, title, failure_reason, failure_summary,
                 started_at, finished_at, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                    $14, $15, now(), now())
            ON CONFLICT (awx_job_id) DO NOTHING
            RETURNING id
            """,
            _parse_awx_job_id(binding.awx_job.job_id),
            binding.awx_job.job_template_id,
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
            binding.failure_summary,
            binding.started_at,
            binding.finished_at,
        )
        if rows:
            return rows[0]["id"]
        return None

    async def _project_afk_run_status(self, afk_run_id: str) -> str:
        """Project one run's status from its binding outcomes (issue #606).

        Reads the outcome multiset for the run and applies the pure-domain
        policy :func:`afk_outcomes.run_status.resolve_afk_run_status`.  The
        caller must already hold the parent ``afk_runs`` lock so the
        multiset is stable while the projection computes.  Legacy rows with
        a NULL outcome carry no trusted signal and are excluded — the
        policy rejects unknown values, so they are never passed to it.
        """
        rows = await self._conn.fetch(
            """
            SELECT outcome FROM execution_bindings
            WHERE afk_run_id = $1 AND outcome IS NOT NULL
            """,
            afk_run_id,
        )
        outcomes = [row.get("outcome") for row in rows if row.get("outcome") is not None]
        return resolve_afk_run_status(outcomes)

    async def _converge_afk_run_status(self, afk_run_id: str) -> None:
        """Converge ``afk_runs.status`` to the binding-driven projection (issue #606).

        Only ``status`` is projected — ``finished_at``, ``outcome_status``,
        ``outcome``, and the change-request columns are never touched.
        Runs inside the caller's transaction with the parent row already
        locked, so the projection and the write are atomic with the binding
        mutation that triggered them.
        """
        projected = await self._project_afk_run_status(afk_run_id)
        await self._conn.execute(
            "UPDATE afk_runs SET status = $2 WHERE afk_run_id = $1",
            afk_run_id,
            projected,
        )

    async def create_or_replay_afk_execution_binding(
        self,
        *,
        awx_job_id: str,
        job_template_id: int,
        provider: Provider | None = None,
        repository: str | None = None,
        resource_number: str | None = None,
        external_session_id: str | None = None,
        external_session_ids: list[str] | None = None,
        outcome: ExecutionOutcome = ExecutionOutcome.COMPLETED,
        source_event_id: str | None = None,
        branch: str | None = None,
        title: str | None = None,
        failure_reason: str | None = None,
        failure_summary: str | None = None,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        trigger_type: str | None = None,
        afk_run_id: str | None = None,
        supplied_fields: set[str] | None = None,
        ulid_source: ULIDSource,
    ) -> CreateAFKExecutionBindingResult:
        """Transactionally create or attach an AFK execution binding.

        **Two-phase lifecycle (issue #590)**: the resource identity is
        optional — ``provider``/``repository``/``resource_number`` may all
        be ``None`` for ``running`` provisioning and for failed/cancelled
        executions that carry no change request.  The change-request and
        session columns are then written as NULL.

        **Lifecycle multiplicity (issue #595)**: when ``afk_run_id`` is
        supplied, the binding attaches to the pre-provisioned lifecycle
        instead of creating a new one — many execution bindings (e.g. a
        failed attempt and a later retry with a new ``awx_job_id``) can
        reference one ``afk_run_id``:

        * **Supplied ``afk_run_id`` exists** — the binding is linked to it
          (no new ``afk_runs`` row) and returns ``is_created=True`` on first
          insert.  When the execution also carries a resource identity, the
          run's change-request relationship is made authoritative via the
          shared lifecycle-binding rule: an unbound lifecycle is bound to
          the execution's change request, a matching bound one is accepted,
          and a differing bound one — or a change request already owned by
          another lifecycle — returns ``is_conflict=True`` without inserting
          (issue #600 review).  ``afk_runs.provider`` records where the
          lifecycle originated (trigger/source provenance) and is
          intentionally independent of the canonical change-request provider,
          so the resource's provider is never gated against the run's store
          provider (issue #600 review).
        * **Supplied ``afk_run_id`` missing** — returns ``run_missing=True``
          without inserting anything (the caller surfaces a 404).
        * **No ``afk_run_id``** — the canonical change-request identity
          (``provider`` / ``repository`` / ``resource_number``) drives
          auto-provisioning.  This path requires a non-None ``provider``
          (the run carries it); the API schema guarantees a resource
          whenever no run is supplied.

          * **First discovery** — a provisional ``afk_runs`` row is created
            transactionally with the binding, authoritative for the change
            request immediately (the change-request columns are written in
            the same INSERT) and returning ``is_created=True``.
          * **Existing lifecycle (PR #600 blocker)** — when the canonical
            change request already owns a lifecycle, the new execution
            *reuses* that ``afk_run_id`` (no second lifecycle) and attaches
            its binding to it, returning ``is_reused=True`` after validating
            the stored tuple through :meth:`_apply_change_request_binding`.
          * **Concurrent first discovery** — the 1:1 rule is enforced with a
            pre-check plus the partial unique index.  The
            ``UniqueViolationError`` loser re-reads the winner lifecycle,
            adopts its ``afk_run_id`` through the same shared binding rule,
            and attaches its execution — never a 409 and never a 500
            (savepoint-wrapped).
          * **Resource-less execution** — the legacy INSERT is preserved;
            the change-request columns stay NULL and are excluded from the
            partial index.

        Replay/conflict semantics are unchanged by the addition:

        * **First call** — creates the binding and returns ``is_created=True``.
        * **Identical replay** (same ``awx_job_id``, same payload) — returns
          the existing ``afk_run_id`` and ``binding_id`` without mutation.
          The supplied ``afk_run_id`` participates in the comparison only
          when the caller supplied one, so a legacy replay that omits it
          never conflicts on the stored auto-created run.
        * **Conflicting replay** (same ``awx_job_id``, different payload,
          or a different supplied ``afk_run_id``) — returns
          ``is_conflict=True`` without mutation.

        **Transactional status convergence (issue #606 / ADR 0027)** — every
        path that touches an existing lifecycle locks the owning ``afk_runs``
        row (``SELECT ... FOR UPDATE``) before the binding is mutated, and
        converges ``afk_runs.status`` to the binding-derived projection in
        the same transaction:

        * the first ``running`` binding moves its run ``pending`` → ``running``;
        * a direct terminal creation converges the run straight to its
          aggregate terminal status (``completed`` / ``failed`` /
          ``cancelled``);
        * a new ``running`` binding on a ``failed`` / ``cancelled`` run
          reopens it to ``running`` (retry, new AWX job identity);
        * a **completed** run rejects any new binding with
          ``is_conflict=True`` without inserting, without touching the
          stored binding history, and without changing ``status``;
        * an identical replay stays idempotent and re-converges its touched
          parent (correcting a stale status) without duplicating or
          changing terminal history.

        Only ``afk_runs.status`` is projected — ``finished_at``,
        ``outcome_status``, ``outcome``, and the change-request columns are
        never touched by the convergence.

        The connection MUST already be in a transaction (the caller owns the
        transaction boundary).  Uses savepoints internally so that a failure
        within this operation rolls back cleanly without leaving orphaned
        ``afk_runs`` rows.

        ``ulid_source`` provides the ULID generator; pass a deterministic
        source in tests for reproducibility.
        """
        numeric_awx_job_id = _parse_awx_job_id(awx_job_id)
        new_ulid = ulid_source.next_ulid()
        # Normalized session attribution (issue #627): the caller supplies the
        # already-normalized collection; a direct repository caller that
        # passes only the legacy singular form falls back to it.
        session_ids = list(
            dict.fromkeys(external_session_ids or [])
        ) if external_session_ids is not None else (
            [external_session_id] if external_session_id else []
        )
        primary_session = session_ids[0] if session_ids else None

        async with self._conn.transaction():
            # Check for an existing binding with this AWX job ID.  The
            # owning AFK Run (when one exists) is locked in the same
            # statement (``FOR UPDATE OF r`` inside the lateral subquery,
            # issue #606 / ADR 0027) so an identical replay converges its
            # parent under the parent lock without a second round-trip —
            # never duplicating or changing terminal binding history.
            # ``FOR UPDATE`` cannot lock the nullable side of an outer
            # join, and the lateral form keeps legacy rows (``afk_run_id``
            # NULL, which carry no aggregation signal) in the result.
            existing = await self._conn.fetchrow(
                """
                SELECT b.id, b.afk_run_id, b.awx_job_id, b.outcome, b.title,
                       b.branch, b.failure_reason, b.failure_summary,
                       b.source_event_id, b.external_session_id,
                       b.started_at, b.finished_at, b.trigger_type
                FROM execution_bindings b
                LEFT JOIN LATERAL (
                    SELECT r.afk_run_id
                    FROM afk_runs r WHERE r.afk_run_id = b.afk_run_id
                    FOR UPDATE OF r
                ) l ON TRUE
                WHERE b.awx_job_id = $1
                """,
                numeric_awx_job_id,
            )

            if existing is not None:
                # Binding already exists — check whether payload matches
                # (idempotent replay) or conflicts.
                existing_payload = {
                    "outcome": existing["outcome"],
                    "trigger_type": existing.get("trigger_type"),
                }
                new_payload = {
                    "outcome": outcome.value,
                    "trigger_type": trigger_type,
                }
                is_match = existing_payload == new_payload
                supplied = supplied_fields
                if supplied is None:
                    # Direct repository callers predate presence tracking; infer
                    # presence from non-null values for that compatibility path.
                    supplied = {
                        field
                        for field, value in {
                            "title": title,
                            "branch": branch,
                            "failure_reason": failure_reason,
                            "failure_summary": failure_summary,
                            "source_event_id": source_event_id,
                            "external_session_id": external_session_id,
                            "started_at": started_at,
                            "finished_at": finished_at,
                            "afk_run_id": afk_run_id,
                        }.items()
                        if value is not None
                    }
                optional_values = {
                    "title": (existing["title"], title),
                    "branch": (existing["branch"], branch),
                    "failure_reason": (existing["failure_reason"], failure_reason),
                    "failure_summary": (existing.get("failure_summary"), failure_summary),
                    "source_event_id": (existing["source_event_id"], source_event_id),
                    "external_session_id": (
                        existing["external_session_id"],
                        primary_session,
                    ),
                    "started_at": (existing.get("started_at"), started_at),
                    "finished_at": (existing.get("finished_at"), finished_at),
                }
                if is_match and any(
                    field in supplied and existing_value != new_value
                    for field, (existing_value, new_value) in optional_values.items()
                ):
                    is_match = False
                # The supplied afk_run_id only participates when the caller
                # supplied one — a legacy replay that omits it never
                # conflicts on the stored auto-created run (issue #595).
                if is_match and afk_run_id is not None:
                    is_match = existing["afk_run_id"] == afk_run_id
                # An identical replay is never rejected (it creates no new
                # binding), but it still converges its touched parent
                # (issue #606 / ADR 0027): the parent lock was already taken
                # by the SELECT above, so the projection is race-free.  The
                # replay never duplicates or changes terminal binding
                # history, while a stale parent status is corrected toward
                # the binding-derived projection.
                if is_match and existing["afk_run_id"] is not None:
                    await self._converge_afk_run_status(existing["afk_run_id"])
                return CreateAFKExecutionBindingResult(
                    afk_run_id=existing["afk_run_id"],
                    binding_id=existing["id"],
                    is_conflict=not is_match,
                )

            # First creation — attach to a pre-provisioned lifecycle when one
            # was supplied, else create the provisional run (legacy behavior).
            run_id = new_ulid
            # True when auto-provisioning adopted an existing lifecycle
            # instead of inserting a fresh afk_runs row (PR #600 blocker).
            reused = False
            if afk_run_id is not None:
                # Lock the owning AFK Run BEFORE any binding mutation
                # (issue #606 / ADR 0027): the parent lock serializes this
                # write against concurrent terminal transitions so the
                # projected status below is stable.
                existing_run = await self._conn.fetchrow(
                    """
                    SELECT afk_run_id, provider, change_request_provider,
                           change_request_repository, change_request_external_id
                    FROM afk_runs WHERE afk_run_id = $1
                    FOR UPDATE
                    """,
                    afk_run_id,
                )
                if existing_run is None:
                    return CreateAFKExecutionBindingResult(
                        afk_run_id=afk_run_id,
                        run_missing=True,
                    )

                # Completed-run rejection (issue #606 / ADR 0027): a
                # completed lifecycle is closed to new AWX Execution
                # Bindings.  The projection is computed under the parent
                # lock held above, so a racing terminal transition commits
                # before this check runs or blocks until it finishes.
                if (
                    await self._project_afk_run_status(afk_run_id)
                    == ExecutionOutcome.COMPLETED.value
                ):
                    return CreateAFKExecutionBindingResult(
                        afk_run_id=afk_run_id,
                        is_conflict=True,
                    )

                # When a resource identity is supplied with the execution,
                # make the referenced lifecycle authoritative for the change
                # request (issue #600 review): an unbound lifecycle is bound,
                # a matching one is accepted idempotently, and a differing one
                # — or a change request owned by another lifecycle — is a
                # conflict.  The execution must never introduce a PR/MR that
                # contradicts its owning lifecycle.  No provider-equality gate
                # is applied: ``afk_runs.provider`` is trigger/source
                # provenance and independent of the canonical change-request
                # provider, which the tuple itself carries (issue #600 review).
                if provider is not None:
                    bind_result = await self._apply_change_request_binding(
                        afk_run_id=afk_run_id,
                        provider=provider,
                        repository=repository,
                        external_id=resource_number,
                        run=existing_run,
                    )
                    if bind_result.is_conflict:
                        return CreateAFKExecutionBindingResult(
                            afk_run_id=afk_run_id,
                            is_conflict=True,
                        )

                run_id = afk_run_id
            else:
                if provider is None:
                    raise ValueError(
                        "provider is required when auto-provisioning an "
                        "afk_run (no afk_run_id supplied)"
                    )
                if repository is not None and resource_number is not None:
                    # Canonical change-request identity present.  The 1:1
                    # invariant is enforced two ways: a pre-check finds the
                    # existing owner, and the partial unique index closes
                    # the race under concurrency.
                    owner = await self._conn.fetchrow(
                        """
                        SELECT afk_run_id, change_request_provider,
                               change_request_repository,
                               change_request_external_id
                        FROM afk_runs
                        WHERE change_request_provider = $1
                          AND change_request_repository = $2
                          AND change_request_external_id = $3
                        FOR UPDATE
                        """,
                        provider.value,
                        repository,
                        resource_number,
                    )
                    if owner is not None:
                        # Completed-run rejection (issue #606 / ADR 0027):
                        # the canonical PR/MR's lifecycle is closed — a new
                        # execution binding is rejected before adoption,
                        # never attached to a completed lifecycle.
                        if (
                            await self._project_afk_run_status(owner["afk_run_id"])
                            == ExecutionOutcome.COMPLETED.value
                        ):
                            return CreateAFKExecutionBindingResult(
                                afk_run_id=owner["afk_run_id"],
                                is_conflict=True,
                            )
                        # The canonical PR/MR already owns a lifecycle —
                        # reuse it and attach this execution instead of
                        # returning 409 (PR #600 blocker).  The shared 1:1
                        # binding rule validates the stored tuple before
                        # adoption; no second afk_runs row is inserted.
                        bind_result = await self._apply_change_request_binding(
                            afk_run_id=owner["afk_run_id"],
                            provider=provider,
                            repository=repository,
                            external_id=resource_number,
                            run=owner,
                        )
                        if bind_result.is_conflict:
                            return CreateAFKExecutionBindingResult(
                                afk_run_id=owner["afk_run_id"],
                                is_conflict=True,
                            )
                        run_id = owner["afk_run_id"]
                        reused = True
                    else:
                        # First discovery — the freshly-created lifecycle is
                        # authoritative for the execution's change request
                        # immediately (issue #600 review).  Catch OUTSIDE the
                        # ``async with`` so the context manager rolls the
                        # savepoint back first (same pattern as
                        # ``_apply_change_request_binding``).
                        try:
                            async with self._conn.transaction():
                                await self._conn.execute(
                                    """
                                    INSERT INTO afk_runs
                                        (afk_run_id, provider, status, title,
                                         started_at, finished_at, outcome_status,
                                         outcome, first_seen_at, last_seen_at,
                                         change_request_provider,
                                         change_request_repository,
                                         change_request_external_id)
                                    VALUES ($1, $2, 'pending', $3, $4, $5, NULL,
                                            NULL, now(), now(), $6, $7, $8)
                                    """,
                                    run_id,
                                    provider.value,
                                    title,
                                    started_at,
                                    finished_at,
                                    provider.value,
                                    repository,
                                    resource_number,
                                )
                        except asyncpg.UniqueViolationError:
                            # A concurrent first discovery of the same change
                            # request won the race — adopt the winner's
                            # lifecycle and attach this execution to it
                            # (PR #600 blocker): never a 409, never a 500.
                            winner = await self._conn.fetchrow(
                                """
                                SELECT afk_run_id, change_request_provider,
                                       change_request_repository,
                                       change_request_external_id
                                FROM afk_runs
                                WHERE change_request_provider = $1
                                  AND change_request_repository = $2
                                  AND change_request_external_id = $3
                                FOR UPDATE
                                """,
                                provider.value,
                                repository,
                                resource_number,
                            )
                            if winner is None:
                                # Cannot happen (the violation means a row
                                # exists), but stay defensive: surface a
                                # clean conflict rather than a crash.
                                return CreateAFKExecutionBindingResult(
                                    afk_run_id=run_id,
                                    is_conflict=True,
                                )
                            # Completed-run rejection (issue #606 / ADR 0027):
                            # the concurrent winner's lifecycle is closed — a
                            # new execution binding is rejected before
                            # adoption.
                            if (
                                await self._project_afk_run_status(
                                    winner["afk_run_id"]
                                )
                                == ExecutionOutcome.COMPLETED.value
                            ):
                                return CreateAFKExecutionBindingResult(
                                    afk_run_id=winner["afk_run_id"],
                                    is_conflict=True,
                                )
                            bind_result = await self._apply_change_request_binding(
                                afk_run_id=winner["afk_run_id"],
                                provider=provider,
                                repository=repository,
                                external_id=resource_number,
                                run=winner,
                            )
                            if bind_result.is_conflict:
                                return CreateAFKExecutionBindingResult(
                                    afk_run_id=winner["afk_run_id"],
                                    is_conflict=True,
                                )
                            run_id = winner["afk_run_id"]
                            reused = True
                else:
                    # Resource-less (or partially-identified) execution —
                    # legacy INSERT preserved; the change-request columns
                    # stay NULL and are excluded from the partial index.
                    await self._conn.execute(
                        """
                        INSERT INTO afk_runs
                            (afk_run_id, provider, status, title, started_at, finished_at,
                             outcome_status, outcome, first_seen_at, last_seen_at)
                        VALUES ($1, $2, 'pending', $3, $4, $5, NULL, NULL, now(), now())
                        """,
                        run_id,
                        provider.value,
                        title,
                        started_at,
                        finished_at,
                    )

            binding_row = await self._conn.fetch(
                """
                INSERT INTO execution_bindings
                    (awx_job_id, job_template_id, external_session_id, provider,
                     repository_url, entity_type, entity_number, outcome,
                     source_event_id, branch, title, failure_reason,
                     failure_summary, started_at, finished_at, afk_run_id,
                     trigger_type, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                        $13, $14, $15, $16, $17, now(), now())
                ON CONFLICT (awx_job_id) DO NOTHING
                RETURNING id
                """,
                numeric_awx_job_id,
                job_template_id,
                primary_session,
                provider.value if provider is not None else None,
                repository,
                EntityType.CHANGE_REQUEST.value
                if resource_number is not None
                else None,
                resource_number,
                outcome.value,
                source_event_id,
                branch,
                title,
                failure_reason,
                failure_summary,
                started_at,
                finished_at,
                run_id,
                trigger_type,
            )

            if binding_row:
                # Persist the afk_run_sessions links in the same transaction
                # when the binding carries an owning lifecycle and session
                # attribution (issue #618, pluralized by issue #627).  One
                # link per unique external session id, in first-occurrence
                # order (the first entry is the primary session).  The
                # internal Gateway session id is resolved best-effort; an
                # unresolved external session id is retained on the link
                # with session_id NULL.
                if session_ids:
                    for session_id_external in session_ids:
                        await self._upsert_session_link(
                            RunSessionLink(
                                afk_run_id=run_id,
                                session_id=await self._resolve_internal_session_id(
                                    session_id_external
                                ),
                                external_session_id=session_id_external,
                                started_at=started_at,
                                finished_at=finished_at,
                            )
                        )
                # Converge the parent toward the binding-derived projection
                # in the same transaction (issue #606 / ADR 0027): the
                # freshly-inserted binding participates in the outcome
                # multiset — first running binding → running, direct
                # terminal creation → its terminal status, retry reopening
                # → running.
                await self._converge_afk_run_status(run_id)
                return CreateAFKExecutionBindingResult(
                    afk_run_id=run_id,
                    binding_id=binding_row[0]["id"],
                    is_created=not reused,
                    is_reused=reused,
                )

            # A binding conflict does not roll back the outer transaction.
            # Remove a provisional run created by this request before reading
            # the winner, otherwise the losing request leaves an orphan run.
            if not reused:
                await self._conn.execute(
                    "DELETE FROM afk_runs WHERE afk_run_id = $1",
                    run_id,
                )
            winner = await self._conn.fetchrow(
                """
                SELECT id, afk_run_id FROM execution_bindings
                WHERE awx_job_id = $1
                """,
                numeric_awx_job_id,
            )
            return CreateAFKExecutionBindingResult(
                afk_run_id=winner["afk_run_id"] if winner else run_id,
                binding_id=winner["id"] if winner else None,
            )

    async def update_execution_binding_terminal(
        self,
        *,
        awx_job_id: str,
        outcome: ExecutionOutcome,
        finished_at: datetime | None = None,
        failure_reason: str | None = None,
        failure_reason_provided: bool | None = None,
        failure_summary: str | None = None,
        failure_summary_provided: bool | None = None,
        external_session_id: str | None = None,
        external_session_ids: list[str] | None = None,
        provider: Provider | None = None,
        repository: str | None = None,
        resource_number: str | None = None,
    ) -> UpdateExecutionBindingResult:
        """Atomically transition one binding from ``running`` to terminal (issue #590).

        The terminal update is the second phase of the two-phase lifecycle:
        the same ``execution_bindings`` row provisioned at AWX start (via
        :meth:`create_or_replay_afk_execution_binding` with ``running``) is
        updated in place to ``completed`` / ``failed`` / ``cancelled``.
        Failed or cancelled updates carry no required change request or
        session.

        **Serialization** — the row is locked with ``SELECT ... FOR UPDATE``
        so concurrent updates for the same AWX job are serialized; a second
        updater re-reads after the first commits and resolves to an
        idempotent replay or a conflict.  The owning ``afk_runs`` row (when
        one exists) is locked in the same statement *before* any binding
        mutation, and ``afk_runs.status`` is converged to the
        binding-derived projection in the same transaction (issue #606 /
        ADR 0027).

        **Non-erasing fill-ins** — ``external_session_id``, the resource
        identity, and ``failure_summary`` are optional: an omitted
        (``None``) field never erases a stored value, a supplied field fills
        a stored NULL, and a supplied field that contradicts a stored
        non-NULL value is a conflict.  Both failure fields are
        presence-aware: omitted values preserve stored metadata, while
        supplied values may fill NULLs during the transition.

        **Lifecycle authority (issue #600 review)** — when the owning
        ``afk_run_id`` is present and the terminal merged state carries a
        resource, the resource is validated against the owning lifecycle's
        change request with the same shared binding rule used on POST: an
        unbound lifecycle is bound, a matching one is accepted, and a
        differing one (or one owned by another lifecycle) is a 409 conflict
        that never mutates the execution row.  The execution can therefore
        never hold a PR/MR that contradicts ``afk_runs``.

        **History is never overwritten** — an already-terminal row is only
        re-observed idempotently (identical payload → unchanged row) or
        rejected (different payload → conflict).  Terminal rows are never
        mutated.

        Returns :class:`UpdateExecutionBindingResult`; the caller re-reads
        the row for the response.  A non-terminal ``outcome`` raises
        ``ValueError`` (the API schema already rejects it).
        """
        if not outcome.is_terminal:
            raise ValueError(
                "update_execution_binding_terminal requires a terminal outcome"
            )
        if failure_reason_provided is None:
            failure_reason_provided = failure_reason is not None
        if failure_summary_provided is None:
            failure_summary_provided = failure_summary is not None
        # Normalized session attribution (issue #627): the API layer supplies
        # the already-normalized, deduplicated collection; a direct repository
        # caller that passes only the legacy singular form falls back to it.
        # ``None`` means "no session attribution supplied" (non-erasing);
        # an empty list is treated the same way here — the API schema rejects
        # empty collections before the repository is reached.
        supplied_session_ids: list[str] | None
        if external_session_ids is not None:
            supplied_session_ids = list(dict.fromkeys(external_session_ids))
        elif external_session_id is not None:
            supplied_session_ids = [external_session_id]
        else:
            supplied_session_ids = None
        fill_session = supplied_session_ids[0] if supplied_session_ids else None
        numeric_awx_job_id = _parse_awx_job_id(awx_job_id)

        async with self._conn.transaction():
            # Lock the binding row (``FOR UPDATE OF b``) and its owning AFK
            # Run (``FOR UPDATE OF r`` inside the lateral subquery) in a
            # single statement (issue #606 / ADR 0027): the parent lock is
            # held before any binding mutation or outcome-multiset read, so
            # a concurrent write to the same parent serializes here.
            # ``FOR UPDATE`` cannot lock the nullable side of an outer
            # join, and the lateral form keeps legacy rows (``afk_run_id``
            # NULL, which carry no aggregation signal) in the result while
            # still locking the binding row itself.
            row = await self._conn.fetchrow(
                """
                SELECT b.id, b.outcome, b.finished_at, b.failure_reason,
                       b.failure_summary, b.external_session_id, b.provider,
                       b.repository_url, b.entity_type, b.entity_number,
                       b.afk_run_id
                FROM execution_bindings b
                LEFT JOIN LATERAL (
                    SELECT r.afk_run_id
                    FROM afk_runs r WHERE r.afk_run_id = b.afk_run_id
                    FOR UPDATE OF r
                ) l ON TRUE
                WHERE b.awx_job_id = $1
                FOR UPDATE OF b
                """,
                numeric_awx_job_id,
            )
            if row is None:
                return UpdateExecutionBindingResult(not_found=True)

            stored_terminal = row["outcome"] in {
                ExecutionOutcome.COMPLETED.value,
                ExecutionOutcome.FAILED.value,
                ExecutionOutcome.CANCELLED.value,
            }
            stored_has_resource = (
                row["provider"] is not None
                and row["repository_url"] is not None
                and row["entity_type"] is not None
                and row["entity_number"] is not None
            )
            requested_has_resource = (
                provider is not None
                and repository is not None
                and resource_number is not None
            )

            # A supplied fill-in contradicts a stored non-null value?
            session_conflict = (
                fill_session is not None
                and row["external_session_id"] is not None
                and row["external_session_id"] != fill_session
            )
            failure_summary_conflict = (
                failure_summary_provided
                and failure_summary is not None
                and row.get("failure_summary") is not None
                and row.get("failure_summary") != failure_summary
            )
            failure_reason_conflict = (
                failure_reason_provided
                and failure_reason is not None
                and row["failure_reason"] is not None
                and row["failure_reason"] != failure_reason
            )
            resource_conflict = (
                requested_has_resource
                and stored_has_resource
                and not (
                    row["provider"] == provider.value
                    and row["repository_url"] == repository
                    and row["entity_type"] == EntityType.CHANGE_REQUEST.value
                    and row["entity_number"] == resource_number
                )
            )

            if stored_terminal:
                identical = (
                    row["outcome"] == outcome.value
                    and row["finished_at"] == finished_at
                    and (
                        not failure_reason_provided
                        or row["failure_reason"] == failure_reason
                    )
                    and not session_conflict
                    and not resource_conflict
                    and not failure_summary_conflict
                )
                if identical:
                    # An identical terminal replay is never rejected and
                    # never mutates terminal history, but it still converges
                    # its touched parent (issue #606 / ADR 0027) — the
                    # parent lock held by the statement above makes the
                    # projection race-free.
                    if row.get("afk_run_id") is not None:
                        await self._converge_afk_run_status(row["afk_run_id"])
                    return UpdateExecutionBindingResult(binding_id=row["id"])
                return UpdateExecutionBindingResult(
                    binding_id=row["id"], is_conflict=True
                )

            # running (or legacy NULL outcome) → terminal transition.
            if (
                session_conflict
                or resource_conflict
                or failure_reason_conflict
                or failure_summary_conflict
            ):
                return UpdateExecutionBindingResult(
                    binding_id=row["id"], is_conflict=True
                )

            new_session = (
                row["external_session_id"]
                if fill_session is None
                else fill_session
            )
            new_failure_reason = (
                row["failure_reason"]
                if not failure_reason_provided
                else failure_reason
            )
            new_failure_summary = (
                row.get("failure_summary")
                if not failure_summary_provided
                else failure_summary
            )
            if requested_has_resource:
                new_provider = provider.value
                new_repository = repository
                new_entity_type = EntityType.CHANGE_REQUEST.value
                new_entity_number = resource_number
            else:
                new_provider = row["provider"]
                new_repository = row["repository_url"]
                new_entity_type = row["entity_type"]
                new_entity_number = row["entity_number"]

            # A completed execution must carry both a change-request identity and
            # a resolved session (issue #600 review).  The stored row may have
            # acquired these during phase one (running provisioning), or the
            # terminal update may supply them as fill-ins.  If neither path
            # produced both, reject the transition.
            if outcome is ExecutionOutcome.COMPLETED:
                has_resource = (
                    new_provider is not None
                    and new_repository is not None
                    and new_entity_type is not None
                    and new_entity_number is not None
                )
                if not has_resource or new_session is None:
                    return UpdateExecutionBindingResult(
                        binding_id=row["id"], is_conflict=True
                    )
                # A completed execution also carries no failure metadata
                # (issue #564).  ``failure_summary`` is a non-erasing fill-in,
                # so a stored value from phase one survives an omitted body —
                # the completed invariant is enforced here after merge: the
                # transition never ends with failure metadata on a completed
                # row.  The API schema rejects explicit failure metadata on
                # completed updates, and this check also protects rows that
                # already carried metadata from phase one.
                if new_failure_reason is not None or new_failure_summary is not None:
                    return UpdateExecutionBindingResult(
                        binding_id=row["id"], is_conflict=True
                    )

            # Lifecycle authority (issue #600 review): a resource on the
            # terminal execution — whether stored from phase one or filled by
            # this update — must be consistent with the owning lifecycle's
            # change request.  Apply the shared binding rule so the execution
            # row can never diverge from ``afk_runs``: an unbound lifecycle is
            # bound to the resource, a matching one is accepted, and a differing
            # one (or a resource owned by another lifecycle) is a conflict that
            # leaves the execution row untouched.  Rows without an owning
            # lifecycle (legacy ``afk_run_id`` NULL) carry no such constraint.
            afk_run_id = row.get("afk_run_id")
            if afk_run_id is not None and new_provider is not None:
                run = await self._conn.fetchrow(
                    """
                    SELECT change_request_provider, change_request_repository,
                           change_request_external_id
                    FROM afk_runs
                    WHERE afk_run_id = $1
                    """,
                    afk_run_id,
                )
                if run is None:
                    # Orphaned run reference — cannot establish authority.
                    return UpdateExecutionBindingResult(
                        binding_id=row["id"], is_conflict=True
                    )
                bind_result = await self._apply_change_request_binding(
                    afk_run_id=afk_run_id,
                    provider=Provider(new_provider),
                    repository=new_repository,
                    external_id=new_entity_number,
                    run=run,
                )
                if bind_result.is_conflict:
                    return UpdateExecutionBindingResult(
                        binding_id=row["id"], is_conflict=True
                    )

            await self._conn.execute(
                """
                UPDATE execution_bindings
                SET outcome = $2,
                    finished_at = $3,
                    failure_reason = $4,
                    external_session_id = $5,
                    provider = $6,
                    repository_url = $7,
                    entity_type = $8,
                    entity_number = $9,
                    failure_summary = $10,
                    updated_at = now()
                WHERE awx_job_id = $1
                """,
                numeric_awx_job_id,
                outcome.value,
                finished_at,
                new_failure_reason,
                new_session,
                new_provider,
                new_repository,
                new_entity_type,
                new_entity_number,
                new_failure_summary,
            )
            # Persist the afk_run_sessions links in the same transaction when
            # the terminal binding carries an owning lifecycle and session
            # attribution — including sessions supplied as terminal fill-ins
            # (issue #618, pluralized by issue #627).  When the terminal
            # update supplies a collection, every unique session id gets a
            # link (the enrich-only upsert never erases an existing link);
            # with no supplied attribution the stored primary session still
            # gets its link.  The internal Gateway session id is resolved
            # best-effort; an unresolved external session id is retained on
            # the link with session_id NULL.
            if row.get("afk_run_id") is not None and (
                new_session is not None or supplied_session_ids
            ):
                link_session_ids = (
                    supplied_session_ids
                    if supplied_session_ids
                    else [new_session]
                )
                for link_external_id in link_session_ids:
                    await self._upsert_session_link(
                        RunSessionLink(
                            afk_run_id=row["afk_run_id"],
                            session_id=await self._resolve_internal_session_id(
                                link_external_id
                            ),
                            external_session_id=link_external_id,
                            started_at=None,
                            finished_at=finished_at,
                        )
                    )
            # Converge the parent toward the binding-derived projection in
            # the same transaction (issue #606 / ADR 0027): the just-
            # transitioned binding participates in the outcome multiset —
            # the final running binding transitioning to terminal converges
            # the run to its aggregate terminal status.
            if row.get("afk_run_id") is not None:
                await self._converge_afk_run_status(row["afk_run_id"])
            return UpdateExecutionBindingResult(
                binding_id=row["id"], is_updated=True
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
            SELECT id, awx_job_id, job_template_id, external_session_id, provider,
                   repository_url, entity_type, entity_number, outcome,
                   source_event_id, branch, title, failure_reason, failure_summary,
                   started_at, finished_at, afk_run_id, trigger_type,
                   external_session_ids AS external_session_ids_json
            FROM execution_bindings
            WHERE awx_job_id = $1
            """,
            _parse_awx_job_id(awx_job_id),
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

        Ordered deterministically by ``created_at ASC, id ASC`` (earliest
        first, with ``id`` as a tie-breaker for same-timestamp rows).
        Different AWX jobs targeting the same GitHub pull request or GitLab
        merge request are both returned.
        """
        rows = await self._conn.fetch(
            """
            SELECT id, awx_job_id, job_template_id, external_session_id, provider,
                   repository_url, entity_type, entity_number, outcome,
                   source_event_id, branch, title, failure_reason, failure_summary,
                   started_at, finished_at, afk_run_id, trigger_type,
                   external_session_ids AS external_session_ids_json
            FROM execution_bindings
            WHERE provider = $1
              AND repository_url = $2
              AND entity_type = $3
              AND entity_number = $4
            ORDER BY created_at ASC, id ASC
            """,
            provider.value,
            repository,
            resource_type.value,
            resource_number,
        )
        return [_row_to_execution_binding(row) for row in rows]

    # ── provisional AFK run lifecycle (issue #589) ──────────────────────

    @staticmethod
    def provisioning_payload_matches(existing: dict, requested: dict) -> bool:
        """Compare an existing provisioning row against a requested payload.

        Returns ``True`` when all provisioning fields match, ``False`` when
        any field differs (indicating a conflict rather than a replay).

        The comparison covers the fields that define the provisioning contract:
        ``repository``, ``trigger_type``, ``title``, and
        ``recovered_from_afk_run_id``.  The idempotency key fields
        (``provider``, ``host``, ``source_event_id``) are assumed to already
        match — the caller is responsible for selecting the existing row by
        those fields.

        Batch-provenance comparison (``first_delivery_id`` + delivery batch)
        is delegated to :meth:`_batch_provenance_matches`, which is the sole
        authority on batch-provenance matching.
        """
        return (
            existing.get("repository") == requested.get("repository")
            and existing.get("trigger_type") == requested.get("trigger_type")
            and existing.get("title") == requested.get("title")
            and existing.get("recovered_from_afk_run_id")
            == requested.get("recovered_from_afk_run_id")
        )

    async def provision_afk_run(
        self,
        *,
        provider: Provider,
        host: str,
        source_event_id: str,
        repository: str,
        trigger_type: TriggerType,
        title: str | None = None,
        recovered_from_afk_run_id: str | None = None,
        deliveries: list[str] | None = None,
        ulid_source: ULIDSource,
    ) -> ProvisionAFKRunResult:
        """Idempotently provision one provisional AFK run lifecycle.

        The idempotency key is ``provider + host + source_event_id``,
        guaranteed by the partial unique index
        ``uq_afk_runs_provisioning_key`` (migration 0039).  Write semantics:

        * **First call** — inserts the ``afk_runs`` row with status
          ``pending``, the source provenance, the repository identity, the
          trigger metadata, the optional recovery reference, and the batch
          provenance (issue #595: ``deliveries[0]`` becomes
          ``first_delivery_id`` and every delivery identity is written to
          ``afk_run_delivery_batches`` in the same transaction); returns
          ``is_created=True``.
        * **Identical replay** — returns the existing ``afk_run_id`` with
          no flags set and issues **no** writes (the batch is compared too:
          a replay with a different or omitted batch is a conflict, never an
          erasure).
        * **Conflicting replay** — the same key with a different payload
          returns ``is_conflict=True`` without mutation.
        * **Missing predecessor** — a ``recovered_from_afk_run_id`` that
          references no existing run returns ``predecessor_missing=True``
          without inserting anything.

        Creating a recovery lifecycle (``recovered_from_afk_run_id`` set)
        never mutates the predecessor row — the predecessor is only read.

        ``deliveries`` is optional for backward compatibility: ``None`` /
        empty means the run carries no batch provenance (legacy behavior).
        Duplicates are deduplicated preserving order so the same logical
        batch always compares equal on replay.

        The connection MUST already be in a transaction (the caller owns
        the transaction boundary).  Uses savepoints internally, mirroring
        :meth:`create_or_replay_afk_execution_binding`; a failed batch write
        rolls the run insert back — no orphan batch rows, no orphan run.
        """
        new_ulid = ulid_source.next_ulid()
        # Batch provenance (issue #595): the ordered, deduplicated identities
        # of the accepted webhook batch.  ``deliveries[0]`` is the first
        # triggering delivery, stored on the run row; every identity becomes
        # a batch row.
        deliveries = list(dict.fromkeys(deliveries or []))
        first_delivery_id = deliveries[0] if deliveries else None

        def _requested_payload() -> dict:
            return {
                "repository": repository,
                "trigger_type": trigger_type.value,
                "title": title,
                "recovered_from_afk_run_id": recovered_from_afk_run_id,
                "first_delivery_id": first_delivery_id,
            }

        async with self._conn.transaction():
            existing = await self._conn.fetchrow(
                """
                SELECT afk_run_id, repository, trigger_type, title,
                       recovered_from_afk_run_id, first_delivery_id
                FROM afk_runs
                WHERE provider = $1 AND host = $2 AND source_event_id = $3
                """,
                provider.value,
                host,
                source_event_id,
            )

            if existing is not None:
                is_match = self.provisioning_payload_matches(
                    existing, _requested_payload()
                )
                if is_match and not await self._batch_provenance_matches(
                    existing["afk_run_id"], first_delivery_id, deliveries
                ):
                    is_match = False
                return ProvisionAFKRunResult(
                    afk_run_id=existing["afk_run_id"],
                    is_conflict=not is_match,
                )

            if recovered_from_afk_run_id is not None:
                predecessor = await self._conn.fetchrow(
                    "SELECT afk_run_id FROM afk_runs WHERE afk_run_id = $1",
                    recovered_from_afk_run_id,
                )
                if predecessor is None:
                    return ProvisionAFKRunResult(
                        afk_run_id=new_ulid,
                        predecessor_missing=True,
                    )

            rows = await self._conn.fetch(
                """
                INSERT INTO afk_runs
                    (afk_run_id, provider, status, title, started_at, finished_at,
                     outcome_status, outcome, host, source_event_id, repository,
                     trigger_type, change_request_provider, change_request_repository,
                     change_request_external_id, recovered_from_afk_run_id,
                     first_delivery_id, first_seen_at, last_seen_at)
                VALUES ($1, $2, 'pending', $3, NULL, NULL, NULL, NULL, $4, $5, $6, $7,
                        NULL, NULL, NULL, $8, $9, now(), now())
                ON CONFLICT (provider, host, source_event_id)
                    WHERE host IS NOT NULL AND source_event_id IS NOT NULL
                    DO NOTHING
                RETURNING afk_run_id
                """,
                new_ulid,
                provider.value,
                title,
                host,
                source_event_id,
                repository,
                trigger_type.value,
                recovered_from_afk_run_id,
                first_delivery_id,
            )
            if rows:
                run_id = rows[0]["afk_run_id"]
                await self._insert_delivery_batch_rows(run_id, deliveries)
                return ProvisionAFKRunResult(
                    afk_run_id=run_id,
                    is_created=True,
                )

            # Lost a concurrent race — re-read the full winner row and compare
            # payloads to distinguish replay from conflict.
            winner = await self._conn.fetchrow(
                """
                SELECT afk_run_id, repository, trigger_type, title,
                       recovered_from_afk_run_id, first_delivery_id
                FROM afk_runs
                WHERE provider = $1 AND host = $2 AND source_event_id = $3
                """,
                provider.value,
                host,
                source_event_id,
            )
            if winner is not None:
                is_match = self.provisioning_payload_matches(
                    winner, _requested_payload()
                )
                if is_match and not await self._batch_provenance_matches(
                    winner["afk_run_id"], first_delivery_id, deliveries
                ):
                    is_match = False
                return ProvisionAFKRunResult(
                    afk_run_id=winner["afk_run_id"],
                    is_conflict=not is_match,
                )
            return ProvisionAFKRunResult(
                afk_run_id=new_ulid,
            )

    async def _insert_delivery_batch_rows(
        self, afk_run_id: str, deliveries: list[str]
    ) -> None:
        """Insert one batch row per contributing delivery identity (issue #595).

        Uses a single batch INSERT with ``unnest`` and ``WITH ORDINALITY``
        so positions are assigned by the database from the array index,
        not by a Python loop — eliminating the ordering mismatch concern
        raised in the PR #596 review.

        Runs inside the provisioning transaction: a failure here rolls the
        run INSERT back with it, so a partially-written batch can never
        outlive its run (no orphan batch rows, no orphan run).
        """
        if not deliveries:
            return
        await self._conn.execute(
            """
            INSERT INTO afk_run_delivery_batches
                (afk_run_id, delivery_id, position, created_at)
            SELECT $1, delivery_id, position, now()
            FROM unnest($2::text[]) WITH ORDINALITY AS t(delivery_id, position)
            ON CONFLICT (afk_run_id, delivery_id) DO NOTHING
            """,
            afk_run_id,
            deliveries,
        )

    async def _batch_provenance_matches(
        self,
        afk_run_id: str,
        first_delivery_id: str | None,
        requested_deliveries: list[str],
    ) -> bool:
        """Compare the stored batch provenance against the requested batch.

        Batch provenance is non-erasing: a replay that omits deliveries
        against a run that carries them (or supplies a different batch) is a
        conflict, never an erasure.  Runs without provenance on either side
        compare equal without touching the database.
        """
        if first_delivery_id is None and not requested_deliveries:
            return True
        rows = await self._conn.fetch(
            """
            SELECT delivery_id FROM afk_run_delivery_batches
            WHERE afk_run_id = $1
            ORDER BY position ASC, id ASC
            """,
            afk_run_id,
        )
        stored = [row["delivery_id"] for row in rows]
        return stored == requested_deliveries

    async def get_afk_run_batch_provenance(
        self, afk_run_id: str
    ) -> tuple[str | None, list[str]]:
        """Return the stored batch provenance for ``afk_run_id`` (issue #595).

        Returns ``(first_delivery_id, delivery_ids)`` — the first triggering
        delivery stored on the run row plus every contributing delivery
        identity of the accepted batch, in stored order.  Unknown runs and
        legacy runs without provenance both return ``(None, [])``.
        """
        rows = await self._conn.fetch(
            """
            SELECT r.first_delivery_id, b.delivery_id
            FROM afk_runs r
            LEFT JOIN afk_run_delivery_batches b ON b.afk_run_id = r.afk_run_id
            WHERE r.afk_run_id = $1
            ORDER BY b.position ASC, b.id ASC
            """,
            afk_run_id,
        )
        if not rows:
            return (None, [])
        first_delivery_id = rows[0].get("first_delivery_id")
        delivery_ids = [
            row.get("delivery_id")
            for row in rows
            if row.get("delivery_id") is not None
        ]
        return (first_delivery_id, delivery_ids)

    async def _apply_change_request_binding(
        self,
        *,
        afk_run_id: str,
        provider: Provider,
        repository: str,
        external_id: str,
        run: asyncpg.Record,
    ) -> ChangeRequestBindingResult:
        """Apply the canonical 1:1 lifecycle<->change_request binding rule.

        The single implementation of the lifecycle invariant (issue #600
        review), shared by :meth:`bind_change_request` and the execution
        write paths (:meth:`create_or_replay_afk_execution_binding` and
        :meth:`update_execution_binding_terminal`):

        * **Unbound lifecycle** — binds the requested change request
          (``is_bound=True``).
        * **Identical replay** — the same identity is already bound; returns
          no flags (no UPDATE issued).
        * **Different identity already bound** — returns ``is_conflict=True``
          without mutation.
        * **Change request owned by another lifecycle** — returns
          ``is_conflict=True`` without mutation (the 1:1 invariant).
        * **Same lifecycle, same change request, concurrent** — when the
          1:1 pre-check finds that the *requested lifecycle itself* is now
          the owner (a concurrent identical bind committed between our read
          and the pre-check), it is re-read, validated, and classified as an
          idempotent replay — never a false ``409`` (issue #600 review).
        * **Lost race** — a concurrent bind of the same lifecycle is re-read
          and classified as replay or conflict, and a concurrent bind of the
          same change request to another lifecycle (a
          ``UniqueViolationError`` on the partial unique index) is rolled
          back to a savepoint and surfaced as ``is_conflict=True`` — never
          a 500.

        ``run`` must be the lifecycle row (with the ``change_request_*``
        columns); callers own loading it, so this helper never issues its
        own ``afk_runs`` SELECT and can reuse the row the host method has
        already fetched.
        """
        existing_tuple = (
            run.get("change_request_provider"),
            run.get("change_request_repository"),
            run.get("change_request_external_id"),
        )
        if existing_tuple[0] is not None:
            is_match = existing_tuple == (provider.value, repository, external_id)
            return ChangeRequestBindingResult(
                afk_run_id=afk_run_id,
                is_conflict=not is_match,
            )

        # 1:1 invariant — the change request must not already belong to
        # another lifecycle.  The partial unique index also enforces this
        # under concurrency; this pre-check turns the common case into a
        # clean conflict instead of a constraint violation.
        other = await self._conn.fetchrow(
            """
            SELECT afk_run_id FROM afk_runs
            WHERE change_request_provider = $1
              AND change_request_repository = $2
              AND change_request_external_id = $3
            """,
            provider.value,
            repository,
            external_id,
        )
        if other is not None:
            if other["afk_run_id"] == afk_run_id:
                # The requested lifecycle itself now owns the change request —
                # a concurrent identical bind committed between our earlier
                # read of ``run`` and this pre-check.  Re-read and validate
                # the complete tuple before treating it as an idempotent
                # replay (issue #600 review); anything else is a genuine
                # conflict (the lifecycle rebinding a different identity).
                run = await self._conn.fetchrow(
                    """
                    SELECT change_request_provider, change_request_repository,
                           change_request_external_id
                    FROM afk_runs
                    WHERE afk_run_id = $1
                    """,
                    afk_run_id,
                )
                is_match = run is not None and (
                    run["change_request_provider"],
                    run["change_request_repository"],
                    run["change_request_external_id"],
                ) == (provider.value, repository, external_id)
                return ChangeRequestBindingResult(
                    afk_run_id=afk_run_id,
                    is_conflict=not is_match,
                )
            return ChangeRequestBindingResult(
                afk_run_id=afk_run_id,
                is_conflict=True,
            )

        # Bind inside a savepoint so a concurrent bind of the same change
        # request to another lifecycle rolls back only the savepoint and
        # surfaces as a clean conflict (the ``uq_afk_runs_change_request_identity``
        # partial unique index is the hard guarantee; the pre-check above just
        # turns the common case into a clean conflict without a constraint
        # violation).  Catching OUTSIDE the ``async with`` lets the context
        # manager roll the savepoint back first — a failed savepoint would
        # otherwise poison the caller's outer transaction.
        try:
            async with self._conn.transaction():
                result = await self._conn.execute(
                    """
                    UPDATE afk_runs
                    SET change_request_provider = $2,
                        change_request_repository = $3,
                        change_request_external_id = $4,
                        last_seen_at = now()
                    WHERE afk_run_id = $1
                      AND change_request_provider IS NULL
                    """,
                    afk_run_id,
                    provider.value,
                    repository,
                    external_id,
                )

                if result == "UPDATE 1":
                    return ChangeRequestBindingResult(
                        afk_run_id=afk_run_id,
                        is_bound=True,
                    )

                # Lost a race against a concurrent bind of this same lifecycle —
                # re-read and classify as replay or conflict.
                run = await self._conn.fetchrow(
                    """
                    SELECT change_request_provider, change_request_repository,
                           change_request_external_id
                    FROM afk_runs
                    WHERE afk_run_id = $1
                    """,
                    afk_run_id,
                )
                is_match = run is not None and (
                    run["change_request_provider"],
                    run["change_request_repository"],
                    run["change_request_external_id"],
                ) == (provider.value, repository, external_id)
                return ChangeRequestBindingResult(
                    afk_run_id=afk_run_id,
                    is_conflict=not is_match,
                )
        except asyncpg.UniqueViolationError:
            # Concurrent bind of the same change request to another lifecycle
            # won the race — the 1:1 invariant holds.
            return ChangeRequestBindingResult(
                afk_run_id=afk_run_id,
                is_conflict=True,
            )

    async def bind_change_request(
        self,
        *,
        afk_run_id: str,
        provider: Provider,
        repository: str,
        external_id: str,
    ) -> ChangeRequestBindingResult:
        """Bind one change request to a provisional lifecycle (idempotent).

        Enforces the 1:1 lifecycle<->change_request invariant (migration
        0039's ``uq_afk_runs_change_request_identity`` partial unique
        index) with explicit conflict signaling:

        * **Unbound lifecycle** — sets the three change-request columns;
          returns ``is_bound=True``.
        * **Identical replay** — the same identity already bound returns
          no flags (no UPDATE issued).
        * **Different identity already bound** — returns
          ``is_conflict=True`` without mutation.
        * **Change request owned by another lifecycle** — returns
          ``is_conflict=True`` without mutation (the 1:1 invariant).
        * **Missing run** — returns ``run_missing=True``.

        Binding is available before review processing — it never depends on
        the correlation engine.  The connection MUST already be in a
        transaction (the caller owns the transaction boundary).

        Delegates the invariant to :meth:`_apply_change_request_binding` so
        the execution write paths enforce the exact same rule.
        """
        async with self._conn.transaction():
            run = await self._conn.fetchrow(
                """
                SELECT afk_run_id, change_request_provider,
                       change_request_repository, change_request_external_id
                FROM afk_runs
                WHERE afk_run_id = $1
                """,
                afk_run_id,
            )
            if run is None:
                return ChangeRequestBindingResult(
                    afk_run_id=afk_run_id,
                    run_missing=True,
                )
            return await self._apply_change_request_binding(
                afk_run_id=afk_run_id,
                provider=provider,
                repository=repository,
                external_id=external_id,
                run=run,
            )

    async def get_afk_run_lifecycle(self, afk_run_id: str) -> AFKRunLifecycle | None:
        """Return the provisional lifecycle for ``afk_run_id``, or ``None``.

        Legacy ``afk_runs`` rows (backfill/reconstruction, migration 0026)
        predate the lifecycle columns; for them ``host``,
        ``source_event_id``, ``repository``, and ``trigger_type`` read back
        as ``None`` (the domain model is lenient on readback).
        """
        row = await self._conn.fetchrow(
            """
            SELECT afk_run_id, provider, status, host, source_event_id, repository,
                   trigger_type, title, change_request_provider,
                   change_request_repository, change_request_external_id,
                   recovered_from_afk_run_id, first_seen_at, last_seen_at
            FROM afk_runs
            WHERE afk_run_id = $1
            """,
            afk_run_id,
        )
        if row is None:
            return None
        return AFKRunLifecycle(
            afk_run_id=row["afk_run_id"],
            provider=Provider(row["provider"]),
            status=row["status"],
            host=row.get("host"),
            source_event_id=row.get("source_event_id"),
            repository=row.get("repository"),
            trigger_type=row.get("trigger_type"),
            title=row.get("title"),
            change_request_provider=(
                Provider(row.get("change_request_provider"))
                if row.get("change_request_provider")
                else None
            ),
            change_request_repository=row.get("change_request_repository"),
            change_request_external_id=row.get("change_request_external_id"),
            recovered_from_afk_run_id=row.get("recovered_from_afk_run_id"),
            first_seen_at=row.get("first_seen_at"),
            last_seen_at=row.get("last_seen_at"),
        )

    async def get_afk_run_by_change_request(
        self,
        *,
        provider: Provider,
        repository: str,
        external_id: str,
    ) -> ChangeRequestLookupResult:
        """Resolve a provider-qualified change-request identity to its owning run.

        Queries ONLY the explicit durable change-request binding columns on
        ``afk_runs`` (``change_request_provider`` / ``change_request_repository``
        / ``change_request_external_id``) — never branch names, issue
        references, commits, titles, timestamps, sessions, AWX jobs,
        correlation tables, or event history (issue #597).

        The 1:1 lifecycle<->change_request invariant (partial unique index
        ``uq_afk_runs_change_request_identity``) guarantees at most one
        owning run.  A query that nonetheless returns more than one row is
        an impossible ownership conflict and is surfaced as
        ``is_conflict=True`` rather than choosing arbitrarily.

        Read-only: issues no writes.
        """
        rows = await self._conn.fetch(
            """
            SELECT afk_run_id
            FROM afk_runs
            WHERE change_request_provider = $1
              AND change_request_repository = $2
              AND change_request_external_id = $3
            """,
            provider.value,
            repository,
            external_id,
        )
        if len(rows) > 1:
            return ChangeRequestLookupResult(is_conflict=True)
        if not rows:
            return ChangeRequestLookupResult()
        return ChangeRequestLookupResult(afk_run_id=rows[0]["afk_run_id"])
