"""Exact deterministic resource<->session association (pure domain).

Derives a many-to-many association between engineering resources and OpenCode
sessions from explicit stable resource references ONLY.  Unlike the
:class:`~afk_outcomes.correlation.CorrelationEngine` (which runs
confidence-ordered rules all the way down to ``temporal_inference``), this
path:

* accepts only :class:`~afk_outcomes.models.SessionResourceReference` inputs —
  explicit references already carrying the full stable resource identity
  (``provider``, ``repository``, ``resource_type``, ``resource_number``) plus
  the session identity and the field that carried them;
* never performs temporal inference, heuristic scoring, or guessing — a link
  exists only because an explicit reference named it;
* records, on every association, the source reference (which session field
  carried the link), so each link is provable and reproducible;
* converges repeated identical references into a single association
  (deduplicated by resource identity + session identity, source fields merged);
* makes no completion/finished claim.

Determinism: identical input produces identical output regardless of input
order — stable sort order, no randomness, no clock dependence.
"""

from __future__ import annotations

from collections.abc import Sequence

from afk_outcomes.models import (
    ASSOCIATION_RESOLVER_VERSION,
    ReferenceSource,
    ResourceSessionAssociation,
    SessionResourceReference,
)


def _merge_source_reference(
    existing: list[ReferenceSource],
    *,
    source_field: str,
    resource_number: str,
) -> list[ReferenceSource]:
    """Merge a source field into the provenance list, deduped and sorted.

    For a given association every reference names the same resource, so the
    ``detail`` (the observed resource value) is constant; deduplication keys on
    the field name.
    """
    merged = {src.field: src for src in existing}
    merged[source_field] = ReferenceSource(field=source_field, detail=resource_number)
    return [merged[field] for field in sorted(merged)]


def derive_exact_associations(
    references: Sequence[SessionResourceReference],
    *,
    resolver_version: str = ASSOCIATION_RESOLVER_VERSION,
) -> list[ResourceSessionAssociation]:
    """Derive resource<->session associations from explicit references only.

    Each distinct (session, resource) pair that has at least one explicit
    reference produces exactly one :class:`ResourceSessionAssociation`.
    Repeated references to the same resource from the same session merge into
    that single association, with ``source_reference`` holding the sorted
    union of the source fields that carried the link.

    The result is sorted deterministically by resource identity
    (``provider``, ``repository``, ``resource_type``, ``resource_number``)
    then session identity, so identical input yields identical output.
    """
    grouped: dict[tuple[str, str, str, str, str], ResourceSessionAssociation] = {}

    for ref in references:
        key = (
            ref.provider.value,
            ref.repository,
            ref.resource_type.value,
            ref.resource_number,
            ref.external_session_id,
        )
        assoc = grouped.get(key)
        if assoc is None:
            assoc = ResourceSessionAssociation(
                session_id=ref.session_id,
                external_session_id=ref.external_session_id,
                provider=ref.provider,
                repository=ref.repository,
                resource_type=ref.resource_type,
                resource_number=ref.resource_number,
                resolver_version=resolver_version,
            )
            grouped[key] = assoc
        elif assoc.session_id is None and ref.session_id is not None:
            # Enrich-only: fill the internal session UUID without erasing.
            assoc.session_id = ref.session_id
        assoc.source_reference = _merge_source_reference(
            assoc.source_reference,
            source_field=ref.source_field,
            resource_number=ref.resource_number,
        )

    return sorted(
        grouped.values(),
        key=lambda a: (
            a.provider.value,
            a.repository,
            a.resource_type.value,
            a.resource_number,
            a.external_session_id,
            a.session_id or "",
        ),
    )
