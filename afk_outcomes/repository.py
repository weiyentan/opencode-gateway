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
  * ``unresolved_correlations`` — enrich-only, same raise/append rules.

Every derived link stores ``correlation_method``, ``correlation_confidence``,
``evidence``, and ``resolver_version``.
"""

from __future__ import annotations

import json

import asyncpg

from afk_outcomes.interfaces import OutcomeRepository
from afk_outcomes.models import (
    AFKRun,
    Correlation,
    CorrelationEvidence,
    EngineeringEntity,
    EngineeringEvent,
    EngineeringOutcome,
    EntityType,
    Provider,
    RunEntityLink,
    RunSessionLink,
    RunStatus,
)

# Version of the correlation resolver that produces the derived links stored
# by this repository.  Bumped whenever link-derivation semantics change.
RESOLVER_VERSION = "1"

# Entity links with this role represent a definitive resolution; correlations
# for any other entity are treated as unresolved.
_RESOLVED_ROLE = "resolved"


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
        await self._conn.execute(
            """
            INSERT INTO engineering_events
                (provider, repository, entity_type, external_id, event_type,
                 occurred_at, provider_event_id, actor, payload, first_ingested_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now())
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

        await self._conn.execute(
            """
            INSERT INTO afk_run_entities
                (afk_run_id, provider, repository, entity_type, external_id, role,
                 correlation_method, correlation_confidence, evidence, resolver_version,
                 superseded_at, first_seen_at, last_seen_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NULL, now(), now())
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
                last_seen_at = now()
            """,
            link.afk_run_id,
            provider,
            repository,
            entity_type,
            external_id,
            link.role,
            correlation_method,
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
            ON CONFLICT (provider, repository, entity_type, external_id, method)
            DO UPDATE SET
                correlation_confidence = GREATEST(
                    unresolved_correlations.correlation_confidence, EXCLUDED.correlation_confidence
                ),
                evidence = unresolved_correlations.evidence || EXCLUDED.evidence,
                resolver_version = COALESCE(
                    EXCLUDED.resolver_version, unresolved_correlations.resolver_version
                ),
                afk_run_id = COALESCE(EXCLUDED.afk_run_id, unresolved_correlations.afk_run_id)
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
            SELECT provider, repository, entity_type, external_id, role,
                   correlation_method, correlation_confidence, evidence
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
            WHERE (entity_type, external_id) IN (
                SELECT entity_type, external_id FROM afk_run_entities
                WHERE afk_run_id = $1 AND superseded_at IS NULL
            )
            """,
            afk_run_id,
        )

        entities: list[EngineeringEntity] = []
        entity_links: list[RunEntityLink] = []
        correlations: list[Correlation] = []
        seen_entities: set[str] = set()

        for row in entity_rows:
            entity_id = f"{row['entity_type']}:{row['external_id']}"
            if entity_id not in seen_entities:
                seen_entities.add(entity_id)
                entities.append(
                    EngineeringEntity(
                        entity_id=entity_id,
                        entity_type=EntityType(row["entity_type"]),
                        provider=Provider(row["provider"]),
                        repository=row["repository"],
                    )
                )
            entity_links.append(
                RunEntityLink(
                    afk_run_id=afk_run_id,
                    entity_id=entity_id,
                    role=row["role"],
                    correlation_confidence=row["correlation_confidence"],
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
