"""Lifecycle-management seam of the ``AsyncpgOutcomeRepository`` (issue #683).

Split out of the former monolithic ``afk_outcomes/repository.py``: the
provisional AFK run lifecycle write path (guarded ``update_afk_run``,
idempotent ``provision_afk_run``, change-request binding, batch provenance)
and the lifecycle read helpers.  The methods are composed onto the facade
class ``AsyncpgOutcomeRepository`` (see the package ``__init__``) and keep
their exact pre-split signatures.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from afk_outcomes.models import (
    AFKRunLifecycle,
    Provider,
    RunStatus,
    TriggerType,
)
from afk_outcomes.repository.executions import ProvisionAFKRunResult
from afk_outcomes.serialization import ULIDSource

# RunStatus values that freeze a run (issue #673): once the lifecycle has
# reached a terminal status it can no longer be mutated through the guarded
# update path — history is never rewritten.
_TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.COMPLETED.value,
        RunStatus.FAILED.value,
        RunStatus.CANCELLED.value,
        RunStatus.TIMED_OUT.value,
    }
)


@dataclass(frozen=True)
class UpdateAFKRunResult:
    """Result of a guarded AFK Run update attempt (issue #673).

    Returned by :meth:`AsyncpgOutcomeRepository.update_afk_run`:

    * ``is_updated=True`` — the supplied title and/or status genuinely
      mutated the stored lifecycle row.
    * ``is_conflict=True`` — the stored run is terminal (frozen) and the
      request would change it; nothing was mutated.
    * ``not_found=True`` — no run with ``afk_run_id`` exists.
    * Idempotent replay (supplied values equal the stored values) sets none
      of the three flags — nothing was mutated.
    """

    afk_run_id: str
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


class _LifecycleRepositoryMixin:
    'Lifecycle-management methods of :class:`AsyncpgOutcomeRepository`.'

    async def update_afk_run(
        self,
        *,
        afk_run_id: str,
        title: str | None = None,
        title_provided: bool = False,
        status: str | None = None,
        status_provided: bool = False,
    ) -> UpdateAFKRunResult:
        """Apply a guarded update to one canonical AFK Run (issue #673).

        The mutable-field set is exactly ``{title, status}``; presence-aware
        flags distinguish an omitted field (stored value left untouched,
        non-erasing) from a supplied one.

        **Serialization** — the ``afk_runs`` row is locked with
        ``SELECT ... FOR UPDATE`` inside a transaction so concurrent guarded
        updates for the same run are serialized; a second updater re-reads
        after the first commits.

        **Lifecycle rules** (evaluated under the lock):

        * ``pending`` runs are provisional and patchable — every RunStatus
          transition (and title change) is applied.
        * Terminal statuses (``completed`` / ``failed`` / ``cancelled`` /
          ``timed_out``) freeze the run — a request that would change it is
          a conflict (``is_conflict``) and nothing is mutated.
        * Identical values are an idempotent no-op — no UPDATE is issued and
          no flag is set.

        Only the ``title`` and ``status`` columns are ever written; linked
        execution bindings, change-request bindings, entity links, and
        session links are untouched by construction.

        Returns :class:`UpdateAFKRunResult`; the caller re-reads the row for
        the response body.
        """
        async with self._conn.transaction():
            row = await self._conn.fetchrow(
                """
                SELECT afk_run_id, status, title
                FROM afk_runs
                WHERE afk_run_id = $1
                FOR UPDATE
                """,
                afk_run_id,
            )
            if row is None:
                return UpdateAFKRunResult(afk_run_id=afk_run_id, not_found=True)

            stored_status = row["status"]

            new_title = title if title_provided else row["title"]
            new_status = status if status_provided else stored_status
            title_changed = title_provided and row["title"] != new_title
            status_changed = status_provided and stored_status != new_status

            if not title_changed and not status_changed:
                # Idempotent replay — nothing to mutate, no UPDATE issued.
                return UpdateAFKRunResult(afk_run_id=afk_run_id)

            if stored_status in _TERMINAL_RUN_STATUSES:
                # A terminal lifecycle is frozen — history is never rewritten.
                return UpdateAFKRunResult(afk_run_id=afk_run_id, is_conflict=True)

            await self._conn.execute(
                """
                UPDATE afk_runs
                SET title = $2,
                    status = $3
                WHERE afk_run_id = $1
                """,
                afk_run_id,
                new_title,
                new_status,
            )
            return UpdateAFKRunResult(afk_run_id=afk_run_id, is_updated=True)

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
