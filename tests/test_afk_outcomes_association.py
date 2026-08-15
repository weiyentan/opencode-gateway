"""Deterministic exact resource<->session association tests (issue #481).

Proves the acceptance criteria:

* a many-to-many association model exists (one resource <-> many sessions,
  one session <-> many resources);
* every association derives ONLY from an explicit stable resource reference —
  never temporal/heuristic inference;
* every association stores its source reference (which session field carried
  the link);
* the same explicit reference repeated converges to a single association
  (idempotent, no duplicates);
* determinism: same session metadata -> same associations; no link is created
  without an explicit reference.

Also verifies the repository write path persists associations idempotently
(``INSERT ... ON CONFLICT DO NOTHING`` keyed on the resource+session identity).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from afk_outcomes import (
    ASSOCIATION_RESOLVER_VERSION,
    AsyncpgOutcomeRepository,
    EntityType,
    Provider,
    ReferenceSource,
    ResourceSessionAssociation,
    SessionResourceReference,
    derive_exact_associations,
)

# ── helpers ─────────────────────────────────────────────────────────────────


def _ref(
    *,
    external_session_id: str = "ses_001",
    source_field: str = "title",
    provider: Provider = Provider.GITHUB,
    repository: str = "acme/backend",
    resource_type: EntityType = EntityType.ISSUE,
    resource_number: str = "437",
    session_id: str | None = None,
) -> SessionResourceReference:
    return SessionResourceReference(
        session_id=session_id,
        external_session_id=external_session_id,
        source_field=source_field,
        provider=provider,
        repository=repository,
        resource_type=resource_type,
        resource_number=resource_number,
    )


# ── Association derivation — pure domain ────────────────────────────────────


def test_no_references_produce_no_associations() -> None:
    assert derive_exact_associations([]) == []


def test_single_reference_produces_one_association_with_provenance() -> None:
    ref = _ref(source_field="project")
    associations = derive_exact_associations([ref])

    assert len(associations) == 1
    assoc = associations[0]
    assert assoc.external_session_id == "ses_001"
    assert assoc.provider is Provider.GITHUB
    assert assoc.repository == "acme/backend"
    assert assoc.resource_type is EntityType.ISSUE
    assert assoc.resource_number == "437"
    # provenance: the link records which session field carried it
    assert assoc.source_reference == [ReferenceSource(field="project", detail="437")]
    assert assoc.resolver_version == ASSOCIATION_RESOLVER_VERSION


def test_same_reference_repeated_converges_to_one_association() -> None:
    """The same explicit reference appearing multiple times is idempotent."""
    ref = _ref()
    associations = derive_exact_associations([ref, ref, ref])

    assert len(associations) == 1


def test_multiple_source_fields_for_same_resource_merge() -> None:
    """Two different fields referencing the same resource -> one association
    with a sorted union of source references (no duplicate link)."""
    associations = derive_exact_associations(
        [_ref(source_field="title"), _ref(source_field="project")]
    )

    assert len(associations) == 1
    assert [s.field for s in associations[0].source_reference] == [
        "project",
        "title",
    ]


def test_one_session_many_resources_is_many_to_many() -> None:
    associations = derive_exact_associations(
        [
            _ref(resource_type=EntityType.ISSUE, resource_number="437"),
            _ref(resource_type=EntityType.CHANGE_REQUEST, resource_number="442"),
            _ref(resource_type=EntityType.ISSUE, resource_number="438"),
        ]
    )

    assert len(associations) == 3
    resource_numbers = sorted(a.resource_number for a in associations)
    assert resource_numbers == ["437", "438", "442"]


def test_one_resource_many_sessions_is_many_to_many() -> None:
    associations = derive_exact_associations(
        [
            _ref(external_session_id="ses_001"),
            _ref(external_session_id="ses_002"),
            _ref(external_session_id="ses_003"),
        ]
    )

    assert len(associations) == 3
    assert sorted(a.external_session_id for a in associations) == [
        "ses_001",
        "ses_002",
        "ses_003",
    ]


def test_distinct_resources_are_not_collapsed() -> None:
    """Different repositories/providers stay distinct associations."""
    associations = derive_exact_associations(
        [
            _ref(repository="acme/backend", provider=Provider.GITHUB),
            _ref(repository="acme/backend", provider=Provider.GITLAB),
            _ref(repository="acme/frontend", provider=Provider.GITHUB),
        ]
    )
    assert len(associations) == 3


def test_determinism_same_input_same_output_in_any_order() -> None:
    refs = [
        _ref(external_session_id="ses_001", source_field="title"),
        _ref(external_session_id="ses_002", source_field="project"),
        _ref(
            external_session_id="ses_001",
            source_field="project",
            resource_type=EntityType.CHANGE_REQUEST,
            resource_number="442",
        ),
    ]
    forward = derive_exact_associations(refs)
    backward = derive_exact_associations(list(reversed(refs)))

    assert forward == backward
    # stable sort order: resource identity (type, number), then session identity
    assert [a.resource_number for a in forward] == ["442", "437", "437"]
    assert [a.external_session_id for a in forward] == [
        "ses_001",
        "ses_001",
        "ses_002",
    ]


def test_no_link_without_explicit_reference() -> None:
    """The derivation is pure: it can only produce a link for a reference it
    was given — there is no input that fabricates an association, and the
    result never exceeds one association per distinct (session, resource)."""
    refs = [
        _ref(external_session_id="ses_001"),
        _ref(external_session_id="ses_002"),
    ]
    associations = derive_exact_associations(refs)
    # one association per (session, resource) pair — nothing extra is invented
    assert len(associations) == 2
    assert {(a.external_session_id, a.resource_number) for a in associations} == {
        ("ses_001", "437"),
        ("ses_002", "437"),
    }


def test_association_model_is_many_to_many_by_construction() -> None:
    """The association model carries independent session and resource keys and
    no run id, no timestamps, no completion/finished claim."""
    assoc = ResourceSessionAssociation(
        external_session_id="ses_001",
        provider=Provider.GITHUB,
        repository="acme/backend",
        resource_type=EntityType.ISSUE,
        resource_number="437",
    )
    assert assoc.source_reference == []
    # no completion/finished claim fields exist on the model
    for field in ("status", "finished", "completed", "outcome"):
        assert field not in ResourceSessionAssociation.model_fields, (
            f"association must not carry {field!r}"
        )


# ── Repository write path — idempotent persistence ──────────────────────────


def _assoc(
    *,
    external_session_id: str = "ses_001",
    session_id: str | None = None,
    provider: Provider = Provider.GITHUB,
    repository: str = "acme/backend",
    resource_type: EntityType = EntityType.ISSUE,
    resource_number: str = "437",
    source_reference: list[ReferenceSource] | None = None,
) -> ResourceSessionAssociation:
    return ResourceSessionAssociation(
        session_id=session_id,
        external_session_id=external_session_id,
        provider=provider,
        repository=repository,
        resource_type=resource_type,
        resource_number=resource_number,
        source_reference=source_reference or [ReferenceSource(field="title", detail="437")],
    )


def _execute_calls(conn: AsyncMock) -> list[str]:
    return [call.args[0] for call in conn.execute.call_args_list]


def _association_calls(conn: AsyncMock) -> list[tuple]:
    return [
        (call.args[0], call.args[1:])
        for call in conn.execute.call_args_list
        if "resource_session_associations" in call.args[0]
    ]


def test_save_associations_uses_conflict_ignore(mock_conn: AsyncMock) -> None:
    repo = AsyncpgOutcomeRepository(mock_conn)
    associations = [_assoc()]

    import asyncio

    asyncio.run(repo.save_associations(associations))

    calls = _association_calls(mock_conn)
    assert calls, "no resource_session_associations insert issued"
    sql = calls[0][0]
    assert (
        "ON CONFLICT (provider, repository, resource_type, resource_number, external_session_id)"
        in sql
    )
    assert "DO NOTHING" in sql
    assert "DO UPDATE" not in sql


def test_save_associations_persists_source_reference(mock_conn: AsyncMock) -> None:
    repo = AsyncpgOutcomeRepository(mock_conn)
    associations = [_assoc(source_reference=[ReferenceSource(field="project", detail="437")])]

    import asyncio

    asyncio.run(repo.save_associations(associations))

    calls = _association_calls(mock_conn)
    sql = calls[0][0]
    assert "source_reference" in sql
    assert "resolver_version" in sql


def test_save_associations_noop_when_empty(mock_conn: AsyncMock) -> None:
    repo = AsyncpgOutcomeRepository(mock_conn)

    import asyncio

    asyncio.run(repo.save_associations([]))

    assert _association_calls(mock_conn) == []


def test_save_associations_never_issues_delete(mock_conn: AsyncMock) -> None:
    repo = AsyncpgOutcomeRepository(mock_conn)

    import asyncio

    asyncio.run(repo.save_associations([_assoc()]))

    for sql in _execute_calls(mock_conn):
        assert "DELETE" not in sql.upper()


def test_derive_then_save_roundtrip_is_idempotent(mock_conn: AsyncMock) -> None:
    """derive_exact_associations + save_associations: a repeated explicit
    reference produces exactly one INSERT (no duplicate, no UPDATE)."""
    repo = AsyncpgOutcomeRepository(mock_conn)
    refs = [_ref(), _ref(), _ref(source_field="project")]

    import asyncio

    associations = derive_exact_associations(refs)
    asyncio.run(repo.save_associations(associations))

    calls = _association_calls(mock_conn)
    assert len(calls) == 1, "the same explicit reference must converge to one INSERT"
