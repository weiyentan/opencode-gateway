"""Raw-asyncpg ``OutcomeRepository`` implementation (issue #448), facade (issue #683).

This is the only database-touching part of the ``afk_outcomes`` package.
It satisfies the :class:`afk_outcomes.interfaces.OutcomeRepository`
protocol (``save`` / ``get``) using asyncpg directly, consistent with the
Gateway's raw-asyncpg data-access convention, and deliberately imports
nothing from ``app`` (enforced mechanically by ``test_afk_outcomes_boundary``).

The former monolithic ``afk_outcomes/repository.py`` module is split into
four focused seam sub-modules behind this thin delegating facade:

* ``crud`` — core CRUD (``save`` / ``save_associations`` / ``record_event`` /
  ``get``) plus the shared serialization helpers and identity constants.
* ``executions`` — AWX execution bindings: the two-phase write path
  (``create_or_replay_afk_execution_binding`` / ``update_execution_binding_terminal``),
  the binding read paths, and the binding result dataclasses.
* ``lifecycle`` — provisional AFK run lifecycle management (``update_afk_run``,
  ``provision_afk_run``, ``bind_change_request``, ``get_afk_run_by_change_request``).
* ``closure_projection`` — the closure-episode projection recompute
  (``recompute_closure_projection``) and rebuild (``rebuild_closure_projection``).

:class:`AsyncpgOutcomeRepository` composes the four seams (mixin classes
defined in each sub-module), so every method — public and private — keeps its
exact pre-split signature and all callers work identically.  Every
module-level name the former ``afk_outcomes.repository`` module exposed is
re-exported here, preserving import compatibility.

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

import asyncpg

from afk_outcomes.interfaces import OutcomeRepository
from afk_outcomes.repository.closure_projection import (
    ClosureRebuildResult,
    _ClosureProjectionRepositoryMixin,
    _CLOSURE_RELEVANT_EVENT_TYPES,
    _closure_fact_issue_keys,
    _decode_jsonb,
    _issue_links_from_payload,
    _to_closure_fact,
)
from afk_outcomes.repository.crud import (
    RESOLVER_VERSION,
    _CrudRepositoryMixin,
    _RUN_LEVEL_ENTITY_TYPE,
    _RESOLVED_ROLE,
    _evidence_json,
    _provider_event_id,
    _source_reference_json,
    _split_entity_id,
)
from afk_outcomes.repository.executions import (
    CreateAFKExecutionBindingResult,
    ProvisionAFKRunResult,
    UpdateExecutionBindingResult,
    _ExecutionBindingsRepositoryMixin,
    _decode_session_ids,
    _parse_awx_job_id,
    _row_to_execution_binding,
)
from afk_outcomes.repository.lifecycle import (
    ChangeRequestBindingResult,
    ChangeRequestLookupResult,
    UpdateAFKRunResult,
    _LifecycleRepositoryMixin,
    _TERMINAL_RUN_STATUSES,
)

__all__ = [
    "AsyncpgOutcomeRepository",
    "ChangeRequestBindingResult",
    "ChangeRequestLookupResult",
    "ClosureRebuildResult",
    "CreateAFKExecutionBindingResult",
    "ProvisionAFKRunResult",
    "RESOLVER_VERSION",
    "UpdateAFKRunResult",
    "UpdateExecutionBindingResult",
    "_CLOSURE_RELEVANT_EVENT_TYPES",
    "_RUN_LEVEL_ENTITY_TYPE",
    "_RESOLVED_ROLE",
    "_TERMINAL_RUN_STATUSES",
    "_closure_fact_issue_keys",
    "_decode_jsonb",
    "_decode_session_ids",
    "_evidence_json",
    "_issue_links_from_payload",
    "_parse_awx_job_id",
    "_provider_event_id",
    "_row_to_execution_binding",
    "_source_reference_json",
    "_split_entity_id",
    "_to_closure_fact",
]


class AsyncpgOutcomeRepository(
    _LifecycleRepositoryMixin,
    _ExecutionBindingsRepositoryMixin,
    _ClosureProjectionRepositoryMixin,
    _CrudRepositoryMixin,
    OutcomeRepository,
):
    """Persist and retrieve AFK runs via a raw asyncpg connection.

    The connection is owned by the caller (acquired from a pool, or a mock
    in unit tests); this repository issues statements against it without
    managing transactions.

    The method bodies live in the four seam sub-modules (``crud``,
    ``executions``, ``lifecycle``, ``closure_projection``) and are composed
    onto this facade class as mixins; the public and private surface is
    identical to the pre-split monolithic ``afk_outcomes/repository.py``.
    """

    def __init__(
        self,
        conn: asyncpg.Connection,
        *,
        resolver_version: str = RESOLVER_VERSION,
    ) -> None:
        self._conn = conn
        self._resolver_version = resolver_version
