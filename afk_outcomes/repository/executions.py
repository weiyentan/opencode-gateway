"""AWX execution-binding seam of the ``AsyncpgOutcomeRepository`` (issue #683).

Split out of the former monolithic ``afk_outcomes/repository.py``: the
two-phase binding write path (``create_or_replay_afk_execution_binding`` /
``update_execution_binding_terminal``), the binding read paths, the row/
payload helpers, and the binding result dataclasses.  The methods are
composed onto the facade class ``AsyncpgOutcomeRepository`` (see the package
``__init__``) and keep their exact pre-split signatures.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

import asyncpg

from afk_outcomes.models import (
    ExecutionBinding,
    ExecutionOutcome,
    EntityType,
    Provider,
    RunSessionLink,
)
from afk_outcomes.run_status import resolve_afk_run_status
from afk_outcomes.serialization import ULIDSource

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


def _decode_session_ids(raw: object) -> list[str]:
    """Decode the JSONB ``external_session_ids`` column value.

    Accepts an already-decoded list or a JSON string (asyncpg returns JSONB
    as ``str`` here), returning the deduplicated collection of non-empty
    string session ids preserving first-occurrence order.  Anything else
    (``None``, legacy rows before migration 0042, malformed payloads)
    decodes to an empty collection.
    """
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except ValueError:
            return []
    else:
        decoded = raw
    if not isinstance(decoded, list):
        return []
    return list(
        dict.fromkeys(s for s in decoded if isinstance(s, str) and s)
    )


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
    session_ids = _decode_session_ids(row.get("external_session_ids_json"))
    if not session_ids:
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


class _ExecutionBindingsRepositoryMixin:
    'AWX execution-binding methods of :class:`AsyncpgOutcomeRepository`.'

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

        .. deprecated::
           Retained as a deprecated compatibility helper (issue #639).
           ADR 0028 supersedes ADR 0027: ``afk_runs.status`` is no longer
           projected from child AWX execution outcomes during binding
           writes.  This helper is not invoked by any binding write path.

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

        .. deprecated::
           Retained as a deprecated compatibility helper (issue #639).
           ADR 0028 supersedes ADR 0027: ``afk_runs.status`` is no longer
           projected from child AWX execution outcomes during binding
           writes.  This helper is not invoked by any binding write path.

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

        **Parent lock (issue #606 / ADR 0027, superseded for status by
        ADR 0028)** — every path that touches an existing lifecycle locks the
        owning ``afk_runs`` row (``SELECT ... FOR UPDATE``) before the
        binding is mutated.  ADR 0028 retires the binding-derived
        ``afk_runs.status`` projection: AWX execution outcomes are historical
        child facts and never close or reopen the AFK Run, so new AWX job IDs
        under an existing ``afk_run_id`` are accepted regardless of prior
        child execution outcomes.  The parent lock is retained for the
        change-request binding rule (:meth:`_apply_change_request_binding`),
        which must serialize concurrent writes to the same lifecycle.

        * the first ``running`` binding attaches normally (201);
        * a direct terminal creation attaches normally (201);
        * a new ``running`` binding on a run whose prior child bindings are
          terminal (``completed`` / ``failed`` / ``cancelled``) is accepted —
          the retry reuses the same ``afk_run_id`` (issue #638);
        * a **completed** prior child binding never rejects a new AWX job ID
          — the completed-run rejection of ADR 0027 is removed (issue #639);
        * an identical replay stays idempotent without duplicating or
          changing terminal history.

        ``_project_afk_run_status()`` / ``_converge_afk_run_status()`` are
        retained as deprecated compatibility helpers (issue #606) but are no
        longer invoked during binding writes.

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
                # binding) and never duplicates or changes terminal binding
                # history.  The binding-derived status convergence of
                # issue #606 / ADR 0027 is retired by ADR 0028 — the parent
                # lock is still taken by the SELECT above for the
                # change-request binding rule, but ``afk_runs.status`` is no
                # longer projected from child execution outcomes.
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

                # ADR 0028 (issue #638/#639): prior child execution outcomes
                # never close the AFK Run to new AWX job IDs.  The
                # completed-run rejection of issue #606 / ADR 0027 is
                # removed — a new binding is accepted under the same
                # ``afk_run_id`` regardless of the projected child status.
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
                        # ADR 0028 (issue #638/#639): the canonical PR/MR's
                        # lifecycle stays open to new AWX job IDs regardless
                        # of prior child execution outcomes — the completed-
                        # run rejection of issue #606 / ADR 0027 is removed.
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
                            # ADR 0028 (issue #638/#639): the concurrent
                            # winner's lifecycle stays open to new AWX job
                            # IDs regardless of prior child outcomes — the
                            # completed-run rejection of issue #606 / ADR
                            # 0027 is removed before winner adoption.
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
                     trigger_type, external_session_ids, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                        $13, $14, $15, $16, $17, $18, now(), now())
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
                # Normalized per-execution attribution (issue #628, extends
                # #627): the JSONB collection column carries every attributed
                # session id so execution cost subtotals can aggregate over
                # the explicit associations.  NULL when there is none —
                # legacy semantics preserved.
                json.dumps(session_ids) if session_ids else None,
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
                # The freshly-inserted binding never re-projects the parent
                # status (ADR 0028 / issue #639): ``afk_runs.status`` is not
                # derived from child AWX execution outcomes, so no converge
                # UPDATE is issued here.  The parent row lock taken earlier
                # still serialized the change-request binding rule.
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
        mutation (issue #606 / ADR 0027).  ADR 0028 retires the
        binding-derived ``afk_runs.status`` projection — the parent is no
        longer converged from child execution outcomes during the terminal
        update (issue #639); the parent lock still serializes the
        change-request binding rule.

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
                       b.afk_run_id,
                       b.external_session_ids AS external_session_ids_json
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
                    # never mutates terminal history.  The binding-derived
                    # status convergence of issue #606 / ADR 0027 is retired
                    # by ADR 0028 — the parent lock held by the statement
                    # above still serializes the change-request binding rule,
                    # but ``afk_runs.status`` is no longer projected from
                    # child execution outcomes.
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

            # Per-execution attribution for the JSONB collection column
            # (issue #628, extends #627): when the terminal update supplies
            # a collection, merge it into the stored one (the enrich-only
            # fill-in never erases stored attribution); otherwise keep the
            # stored value untouched (NULL stays NULL for legacy rows).
            stored_session_ids = _decode_session_ids(
                row.get("external_session_ids_json")
            )
            if supplied_session_ids:
                new_session_ids = list(
                    dict.fromkeys([*stored_session_ids, *supplied_session_ids])
                )
            else:
                new_session_ids = stored_session_ids

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
                    external_session_ids = $11,
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
                json.dumps(new_session_ids) if new_session_ids else None,
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
            # The transitioned binding never re-projects the parent status
            # (ADR 0028 / issue #639): ``afk_runs.status`` is not derived
            # from child AWX execution outcomes, so no converge UPDATE is
            # issued here.
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

    async def list_execution_bindings_by_afk_run_id(
        self, afk_run_id: str
    ) -> list[ExecutionBinding]:
        """Return every execution binding attached to one AFK run lifecycle.

        Many execution bindings can reference one ``afk_run_id`` (a failed
        attempt and a later retry with a new ``awx_job_id`` — issue #595).
        Ordered deterministically by ``created_at ASC, id ASC`` (earliest
        first, with ``id`` as a tie-breaker for same-timestamp rows),
        mirroring :meth:`list_execution_bindings_for_resource`.  A run with
        no bindings reads back as an empty list.
        """
        rows = await self._conn.fetch(
            """
            SELECT id, awx_job_id, job_template_id, external_session_id, provider,
                   repository_url, entity_type, entity_number, outcome,
                   source_event_id, branch, title, failure_reason, failure_summary,
                   started_at, finished_at, afk_run_id, trigger_type,
                   external_session_ids AS external_session_ids_json
            FROM execution_bindings
            WHERE afk_run_id = $1
            ORDER BY created_at ASC, id ASC
            """,
            afk_run_id,
        )
        return [_row_to_execution_binding(row) for row in rows]
