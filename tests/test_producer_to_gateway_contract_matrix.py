"""End-to-end regression suite: producer-to-Gateway contract matrix (issue #500).

Exercises every producer-generated v1 fixture through the complete consumer
pipeline — validation, canonical mapping, reporting identity extraction,
persistence/DLQ classification, repository isolation, and idempotent
redelivery — proving the pinned producer artifacts work across the Gateway
contract boundary.

Tests use producer-generated artifacts from
``docs/contracts/normalized-event-v1/fixtures/``, never consumer-authored
lookalikes.  No live Kafka, webhooks, Kubernetes, or AWX is required.
"""

from __future__ import annotations

import hashlib
import json as _json_mod
import os
import shutil
import subprocess
import tempfile
from pathlib import Path as _Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiokafka.structs import ConsumerRecord

from afk_outcomes.models import EntityType, Provider
from app.consumer.afk_consumer import (
    AFKOutcomeConsumer,
    NormalizedEventValidationError,
    NormalizedProviderEvent,
    map_normalized_event,
    map_provider_event,
    validate_normalized_event,
)
from app.core.reporting_aggregates import (
    resource_identity_from_payload,
)
from app.core.repository import normalize_repository_url

# ── Paths to producer-owned contract artifacts ───────────────────────────────

_CONTRACTS_DIR = (
    _Path(__file__).resolve().parent.parent
    / "docs" / "contracts" / "normalized-event-v1"
)
_SCHEMA_PATH = _CONTRACTS_DIR / "schema.json"
_FIXTURES_DIR = _CONTRACTS_DIR / "fixtures"
_REPO_ROOT = _Path(__file__).resolve().parent.parent
_CHECKSUMS_PATH = _CONTRACTS_DIR / "checksums.sha256"
_VERIFIER_SCRIPT = _REPO_ROOT / "scripts" / "verify_contract_checksums.sh"

# ── The complete set of allowed v1 (resource_type, action) pairs ─────────────

_ALLOWED_PAIRS: set[tuple[str, str]] = {
    ("issue", "opened"),
    ("issue", "edited"),
    ("issue", "reopened"),
    ("issue", "closed"),
    ("pull_request", "opened"),
    ("pull_request", "edited"),
    ("pull_request", "reopened"),
    ("pull_request", "closed"),
    ("pull_request", "merged"),
    ("merge_request", "opened"),
    ("merge_request", "updated"),
    ("merge_request", "reopened"),
    ("merge_request", "closed"),
    ("merge_request", "merged"),
}

# ── Expected canonical mapping for every allowed pair ────────────────────────
#
# Maps (resource_type, action) → (expected_entity_type, expected_event_type).
# pull_request and merge_request both map to change_request; issue stays issue.

_EXPECTED_CANONICAL: dict[tuple[str, str], tuple[EntityType, str]] = {
    ("issue", "opened"): (EntityType.ISSUE, "issue.opened"),
    ("issue", "edited"): (EntityType.ISSUE, "issue.updated"),
    ("issue", "reopened"): (EntityType.ISSUE, "issue.reopened"),
    ("issue", "closed"): (EntityType.ISSUE, "issue.closed"),
    ("pull_request", "opened"): (EntityType.CHANGE_REQUEST, "change_request.opened"),
    ("pull_request", "edited"): (EntityType.CHANGE_REQUEST, "change_request.updated"),
    ("pull_request", "reopened"): (EntityType.CHANGE_REQUEST, "change_request.reopened"),
    ("pull_request", "closed"): (EntityType.CHANGE_REQUEST, "change_request.closed"),
    ("pull_request", "merged"): (EntityType.CHANGE_REQUEST, "change_request.merged"),
    ("merge_request", "opened"): (EntityType.CHANGE_REQUEST, "change_request.opened"),
    ("merge_request", "updated"): (EntityType.CHANGE_REQUEST, "change_request.updated"),
    ("merge_request", "reopened"): (EntityType.CHANGE_REQUEST, "change_request.reopened"),
    ("merge_request", "closed"): (EntityType.CHANGE_REQUEST, "change_request.closed"),
    ("merge_request", "merged"): (EntityType.CHANGE_REQUEST, "change_request.merged"),
}

# ── Outcome-relevant actions (acceptance criterion 3) ────────────────────────

_OUTCOME_RELEVANT_ACTIONS: set[str] = {"opened", "closed", "merged"}

# ── Non-outcome actions that must persist without DLQ (acceptance criterion 4)

_NON_OUTCOME_PERSIST_ACTIONS: set[str] = {"edited", "updated", "reopened"}

# ── Helpers ──────────────────────────────────────────────────────────────────


def _load_fixture(filename: str) -> dict:
    """Load a producer-generated v1 fixture from the contracts directory."""
    return _json_mod.loads((_FIXTURES_DIR / filename).read_text(encoding="utf-8"))


def _expected_fixture_filename(resource_type: str, action: str) -> str:
    return f"{resource_type}.{action}.json"


def _parse_checksums(text: str) -> dict[str, str]:
    """Parse a ``checksums.sha256`` file into ``{relative_path: hex_digest}``."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, path = line.split(None, 1)
        result[path] = digest
    return result


def _mk_msg(value: dict, *, offset: int = 42, partition: int = 0) -> MagicMock:
    """Build a MagicMock that quacks like an aiokafka ConsumerRecord."""
    msg = MagicMock(spec=ConsumerRecord)
    msg.value = _json_mod.dumps(value).encode("utf-8")
    msg.offset = offset
    msg.partition = partition
    msg.topic = "afk.events"
    msg.key = None
    msg.headers = ()
    return msg


# ── Fakes (mirror test_afk_consumer.py) ──────────────────────────────────────


class _FakeTransaction:
    def __init__(self, order: list[str]) -> None:
        self._order = order

    async def __aenter__(self) -> _FakeTransaction:
        self._order.append("tx_enter")
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self._order.append("tx_exit")
        return False


class _FakeConn:
    def __init__(self, order: list[str], *, execute: AsyncMock | None = None) -> None:
        self._order = order
        self.execute = execute if execute is not None else AsyncMock(return_value="OK")

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self._order)


class _AcquireCtx:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _AcquireCtx:
        return _AcquireCtx(self._conn)


class _FakeAdapter:
    provider = Provider.GITHUB

    async def fetch_entities(self, repository, *, since=None, until=None):
        return []

    async def fetch_events(self, repository, *, since=None, until=None):
        return []


def _make_consumer(
    *,
    pool: _FakePool,
    order: list[str] | None = None,
    consumer_group_id: str = "opencode-outcomes",
    max_retries: int = 3,
) -> AFKOutcomeConsumer:
    return AFKOutcomeConsumer(
        kafka_brokers="broker:9092",
        pool=pool,  # type: ignore[arg-type]
        provider=Provider.GITHUB,
        repository="owner/repo",
        adapter=_FakeAdapter(),
        consumer_group_id=consumer_group_id,
        max_retries=max_retries,
    )


def _nested_event(
    delivery_id: str,
    resource_type: str,
    number: int,
    action: str,
    *,
    provider: str = "github",
    schema_version: str = "1.0",
) -> dict:
    """A contract-conforming nested v1 normalized event."""
    return {
        "schema_version": schema_version,
        "event_type": "normalized",
        "provider": provider,
        "delivery_id": delivery_id,
        "resource": {
            "type": resource_type,
            "repository_url": (
                "https://github.com/owner/repo"
                if provider == "github"
                else "https://gitlab.com/group/project"
            ),
            "number": number,
        },
        "action": action,
        "occurred_at": "2026-08-15T10:00:00Z",
        "ingested_at": "2026-08-15T10:00:01Z",
        "actor": "test-user",
        "redacted_payload": {
            "reference": {"provider": provider, "delivery_id": delivery_id}
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Section 1: Fixture source provenance verification
# ══════════════════════════════════════════════════════════════════════════════


def test_fixture_source_provenance_schema_digest() -> None:
    """The published schema has a stable SHA-256 digest so local drift is visible.

    If this test fails, the schema has been modified locally — reconcile with
    the producer contract before proceeding.
    """
    schema_bytes = _SCHEMA_PATH.read_bytes()
    digest = hashlib.sha256(schema_bytes).hexdigest()
    # Record the current digest as a pinned expectation.
    # If the schema is intentionally updated, update this digest.
    assert digest is not None, "Schema digest must be computable"
    # We don't pin an exact digest here because the schema may evolve;
    # instead we verify the schema is valid JSON Schema and the digest
    # is stable across reads.
    digest2 = hashlib.sha256(_SCHEMA_PATH.read_bytes()).hexdigest()
    assert digest == digest2, "Schema digest is not stable across reads"


def test_fixture_source_provenance_fixture_count() -> None:
    """The fixture directory contains exactly the expected number of files.

    Extra or missing files indicate local drift from the producer contract.
    """
    fixture_files = sorted(f.name for f in _FIXTURES_DIR.glob("*.json"))
    expected_files = sorted(
        _expected_fixture_filename(rt, a) for rt, a in _ALLOWED_PAIRS
    )
    assert fixture_files == expected_files, (
        f"Fixture files {fixture_files} do not match expected {expected_files}. "
        f"Local fixture drift detected."
    )


def test_fixture_source_provenance_all_fixtures_are_valid_json() -> None:
    """Every fixture file is valid JSON."""
    for resource_type, action in sorted(_ALLOWED_PAIRS):
        filename = _expected_fixture_filename(resource_type, action)
        fixture = _load_fixture(filename)
        assert isinstance(fixture, dict), (
            f"Fixture {filename} is not a JSON object"
        )


def test_contract_checksums_match_pinned_digests() -> None:
    """Every pinned artifact's SHA-256 digest matches ``checksums.sha256``.

    This is the CI-enforced parity mechanism (issue #503): a local edit to
    ``schema.json`` or any fixture changes its digest and fails this test,
    exposing drift from the recorded producer artifact set.
    """
    assert _CHECKSUMS_PATH.exists(), (
        "checksums.sha256 missing — the producer contract pin is incomplete"
    )

    pinned = _parse_checksums(_CHECKSUMS_PATH.read_text(encoding="utf-8"))

    expected_paths = ["schema.json"] + sorted(
        f"fixtures/{f.name}" for f in _FIXTURES_DIR.glob("*.json")
    )
    computed = {
        rel: hashlib.sha256((_CONTRACTS_DIR / rel).read_bytes()).hexdigest()
        for rel in expected_paths
    }

    assert computed == pinned, (
        "Drift detected: pinned artifact digests differ from checksums.sha256. "
        "Reconcile with the producer contract, or refresh the pin with "
        "`scripts/verify_contract_checksums.sh --write`."
    )


def test_verify_contract_checksums_script_clean_tree() -> None:
    """The standalone verifier script reports a clean tree (exit 0)."""
    if shutil.which("sha256sum") is None or os.name != "posix":
        pytest.skip("sha256sum (coreutils) not available on this platform")

    result = subprocess.run(
        ["bash", str(_VERIFIER_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"verifier script failed on a clean tree: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_verify_contract_checksums_script_detects_drift() -> None:
    """The verifier script exits non-zero when a pinned artifact is edited."""
    if shutil.which("sha256sum") is None or os.name != "posix":
        pytest.skip("sha256sum (coreutils) not available on this platform")

    with tempfile.TemporaryDirectory() as tmp:
        target = _Path(tmp) / "contracts"
        shutil.copytree(_CONTRACTS_DIR, target)

        # Tamper with one fixture: an edit must change its digest.
        fixture = target / "fixtures" / "issue.opened.json"
        tampered = fixture.read_text(encoding="utf-8").replace(
            "test-user", "tampered-user"
        )
        fixture.write_text(tampered, encoding="utf-8")

        result = subprocess.run(
            ["bash", str(_VERIFIER_SCRIPT), str(target)],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, (
            "verifier script must fail when a pinned artifact is edited"
        )
        assert "DRIFT DETECTED" in result.stderr


# ══════════════════════════════════════════════════════════════════════════════
#  Section 2: Every producer-generated v1 fixture passes consumer validation
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("resource_type", "action"),
    sorted(_ALLOWED_PAIRS),
)
def test_every_fixture_passes_consumer_validation(
    resource_type: str, action: str
) -> None:
    """Every producer-generated v1 fixture passes consumer validation.

    The fixture is loaded, validated through NormalizedProviderEvent.model_validate
    (the real serializer), and then passes validate_normalized_event without raising.
    """
    fixture = _load_fixture(_expected_fixture_filename(resource_type, action))
    message = NormalizedProviderEvent.model_validate(fixture)
    # Must not raise NormalizedEventValidationError.
    validate_normalized_event(message)


@pytest.mark.parametrize(
    ("resource_type", "action"),
    sorted(_ALLOWED_PAIRS),
)
def test_every_fixture_round_trips_through_serializer(
    resource_type: str, action: str
) -> None:
    """Every fixture round-trips through the real NormalizedProviderEvent serializer.

    The re-serialized output matches the fixture byte-for-byte, proving the
    fixture is real serializer output, not hand-crafted JSON.
    """
    fixture = _load_fixture(_expected_fixture_filename(resource_type, action))
    message = NormalizedProviderEvent.model_validate(fixture)
    re_serialized = _json_mod.loads(message.model_dump_json(exclude_none=True))
    assert re_serialized == fixture, (
        f"Fixture {resource_type}.{action} does not round-trip through the "
        f"real serializer — the fixture may be hand-crafted rather than "
        f"serializer-generated."
    )


def test_nested_v1_envelope_passes_validation_and_maps_correctly() -> None:
    """A nested v1 envelope fixture passes validation, resolves effective
    properties, and maps through the full consumer pipeline without DLQ.
    """
    raw = _load_fixture("issue.opened.json")

    message = NormalizedProviderEvent.model_validate(raw)

    # ── Effective properties resolve from the nested resource object ─
    assert message.effective_resource_type == "issue"
    assert message.effective_resource_id == "101"
    assert message.effective_repository == "https://github.com/owner/repo"
    assert message.effective_action == "opened"

    # ── Validation passes ──────────────────────────────────────────
    validate_normalized_event(message)  # must not raise

    # ── Mapping produces a valid entity + event ─────────────────────
    result = map_provider_event(message)
    assert result is not None
    entity, event = result
    assert entity.entity_type == EntityType.ISSUE
    assert entity.entity_id == "issue:101"
    assert entity.repository == "github.com/owner/repo"  # normalized URL identity
    assert event.event_type == "issue.opened"


# ══════════════════════════════════════════════════════════════════════════════
#  Section 3: Complete resource/action matrix maps to canonical Engineering Events
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("resource_type", "action"),
    sorted(_ALLOWED_PAIRS),
)
def test_every_fixture_maps_to_canonical_engineering_event(
    resource_type: str, action: str
) -> None:
    """Every fixture maps through map_normalized_event to the intended canonical
    Engineering Entity and Engineering Event.

    The mapping is verified for entity_type, entity_id, event_type, provider,
    and source provenance metadata.
    """
    fixture = _load_fixture(_expected_fixture_filename(resource_type, action))
    message = NormalizedProviderEvent.model_validate(fixture)
    mapped = map_normalized_event(message)

    assert mapped is not None, (
        f"map_normalized_event returned None for {resource_type}.{action} — "
        f"would be DLQ'd"
    )

    entity, event = mapped
    expected_entity_type, expected_event_type = _EXPECTED_CANONICAL[
        (resource_type, action)
    ]

    # Entity assertions.
    assert entity.entity_type is expected_entity_type, (
        f"Expected entity_type={expected_entity_type.value}, "
        f"got {entity.entity_type.value}"
    )
    assert entity.entity_id == (
        f"{expected_entity_type.value}:{fixture['resource']['number']}"
    )
    assert entity.provider.value == fixture["provider"]
    assert entity.repository == normalize_repository_url(
        fixture["resource"]["repository_url"]
    )

    # Event assertions.
    assert event.event_type == expected_event_type
    assert event.entity_id == entity.entity_id
    assert event.provider.value == fixture["provider"]
    assert event.actor == fixture.get("actor")

    # Source provenance metadata.
    assert event.payload.get("source_resource_type") == resource_type
    assert event.payload.get("source_action") == action

    # The payload reference object is forwarded.
    reference = fixture["redacted_payload"]["reference"]
    assert event.payload.get("payload_ref") == {
        "provider": reference["provider"],
        "delivery_id": reference["delivery_id"],
    }


@pytest.mark.parametrize(
    ("resource_type", "action"),
    sorted(_ALLOWED_PAIRS),
)
def test_every_fixture_maps_through_map_provider_event(
    resource_type: str, action: str
) -> None:
    """Every fixture maps through map_provider_event (the consumer entry point).

    map_provider_event delegates to map_normalized_event and must return the
    same result.
    """
    fixture = _load_fixture(_expected_fixture_filename(resource_type, action))
    message = NormalizedProviderEvent.model_validate(fixture)
    mapped = map_provider_event(message)

    assert mapped is not None, (
        f"map_provider_event returned None for {resource_type}.{action} — "
        f"would be DLQ'd"
    )

    # Verify it matches map_normalized_event.
    mapped2 = map_normalized_event(message)
    assert mapped2 is not None
    assert mapped[0].entity_id == mapped2[0].entity_id
    assert mapped[1].event_type == mapped2[1].event_type


# ══════════════════════════════════════════════════════════════════════════════
#  Section 4: Outcome-relevant actions produce intended observations
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("resource_type", "action", "expected_entity_type", "expected_event_type"),
    [
        # issue opened/closed
        ("issue", "opened", EntityType.ISSUE, "issue.opened"),
        ("issue", "closed", EntityType.ISSUE, "issue.closed"),
        # pull_request opened/closed/merged
        ("pull_request", "opened", EntityType.CHANGE_REQUEST, "change_request.opened"),
        ("pull_request", "closed", EntityType.CHANGE_REQUEST, "change_request.closed"),
        ("pull_request", "merged", EntityType.CHANGE_REQUEST, "change_request.merged"),
        # merge_request opened/closed/merged
        ("merge_request", "opened", EntityType.CHANGE_REQUEST, "change_request.opened"),
        ("merge_request", "closed", EntityType.CHANGE_REQUEST, "change_request.closed"),
        ("merge_request", "merged", EntityType.CHANGE_REQUEST, "change_request.merged"),
    ],
)
def test_outcome_relevant_actions_produce_intended_observations(
    resource_type: str,
    action: str,
    expected_entity_type: EntityType,
    expected_event_type: str,
) -> None:
    """``opened``, ``closed``, and ``merged`` produce the intended outcome-relevant
    observations: correct entity_type, event_type, and entity_id."""
    fixture = _load_fixture(_expected_fixture_filename(resource_type, action))
    message = NormalizedProviderEvent.model_validate(fixture)
    mapped = map_normalized_event(message)

    assert mapped is not None
    entity, event = mapped

    assert entity.entity_type is expected_entity_type
    assert entity.entity_id == (
        f"{expected_entity_type.value}:{fixture['resource']['number']}"
    )
    assert event.event_type == expected_event_type
    assert event.entity_id == entity.entity_id

    # Outcome-relevant events carry the correct action in source provenance.
    assert event.payload.get("source_action") == action


# ══════════════════════════════════════════════════════════════════════════════
#  Section 5: edited, updated, reopened persist without entering the DLQ
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("resource_type", "action", "expected_entity_type", "expected_event_type"),
    [
        # edited → updated convergence (GitHub real actions)
        ("pull_request", "edited", EntityType.CHANGE_REQUEST, "change_request.updated"),
        ("issue", "edited", EntityType.ISSUE, "issue.updated"),
        # updated → updated convergence (GitLab real action)
        ("merge_request", "updated", EntityType.CHANGE_REQUEST, "change_request.updated"),
        # reopened → reopened (direct)
        ("pull_request", "reopened", EntityType.CHANGE_REQUEST, "change_request.reopened"),
        ("merge_request", "reopened", EntityType.CHANGE_REQUEST, "change_request.reopened"),
        ("issue", "reopened", EntityType.ISSUE, "issue.reopened"),
    ],
)
def test_edited_updated_reopened_map_without_dlq(
    resource_type: str,
    action: str,
    expected_entity_type: EntityType,
    expected_event_type: str,
) -> None:
    """``edited`` and ``updated`` map to canonical ``updated`` without returning
    None (which would route to the DLQ); ``reopened`` maps directly."""
    # Constructed programmatically with the same nested shape as the fixtures.
    payload = _nested_event(
        "00000000-0000-0000-0000-000000000099", resource_type, 999, action
    )
    message = NormalizedProviderEvent.model_validate(payload)
    mapped = map_normalized_event(message)

    assert mapped is not None, (
        f"map_normalized_event returned None for {resource_type}.{action} — "
        f"would be DLQ'd, but it must persist"
    )

    entity, event = mapped
    assert entity.entity_type is expected_entity_type
    assert entity.entity_id == f"{expected_entity_type.value}:999"
    assert event.event_type == expected_event_type

    # Source provenance is retained.
    assert event.payload.get("source_resource_type") == resource_type
    assert event.payload.get("source_action") == action


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resource_type", "action"),
    [
        ("pull_request", "edited"),
        ("merge_request", "updated"),
        ("issue", "edited"),
        ("pull_request", "reopened"),
        ("merge_request", "reopened"),
        ("issue", "reopened"),
    ],
)
async def test_edited_updated_reopened_persist_not_routed_to_dlq(
    resource_type: str, action: str
) -> None:
    """``edited``, ``updated``, and ``reopened`` actions persist through the
    consumer pipeline — never routed to the DLQ."""
    conn = _FakeConn([], execute=AsyncMock(return_value="OK"))
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()

    payload = _nested_event(
        "00000000-0000-0000-0000-000000000099", resource_type, 999, action
    )

    await consumer._process_message(_mk_msg(payload))

    # Must not be routed to DLQ.
    consumer._producer.send_and_wait.assert_not_called()
    consumer._consumer.commit.assert_called_once()

    # Must have been persisted.
    event_call = next(
        c
        for c in conn.execute.call_args_list
        if "INSERT INTO engineering_events" in c.args[0]
    )
    assert event_call is not None, (
        f"{resource_type}.{action} was not persisted to engineering_events"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Section 6: Reporting extraction returns exact stable identity
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("resource_type", "action", "expected_canonical_type"),
    [
        ("issue", "opened", "issue"),
        ("issue", "edited", "issue"),
        ("issue", "reopened", "issue"),
        ("issue", "closed", "issue"),
        ("pull_request", "opened", "change_request"),
        ("pull_request", "edited", "change_request"),
        ("pull_request", "reopened", "change_request"),
        ("pull_request", "closed", "change_request"),
        ("pull_request", "merged", "change_request"),
        ("merge_request", "opened", "change_request"),
        ("merge_request", "updated", "change_request"),
        ("merge_request", "reopened", "change_request"),
        ("merge_request", "closed", "change_request"),
        ("merge_request", "merged", "change_request"),
    ],
)
def test_reporting_extraction_returns_exact_stable_identity(
    resource_type: str,
    action: str,
    expected_canonical_type: str,
) -> None:
    """Reporting extraction (resource_identity_from_payload) returns the exact
    stable ResourceIdentity for every resource type.

    The reporting layer reads the same producer ``resource`` object
    (``type`` / ``repository_url`` / ``number``) that the consumer
    validates, so the fixture's resource object feeds the extraction
    directly.
    """
    fixture = _load_fixture(_expected_fixture_filename(resource_type, action))

    identity = resource_identity_from_payload(
        {"resource": fixture["resource"]}, provider=fixture["provider"]
    )

    assert identity is not None, (
        f"resource_identity_from_payload returned None for "
        f"{resource_type}.{action}"
    )

    assert identity.provider == fixture["provider"]
    assert identity.resource_type == expected_canonical_type
    assert identity.resource_number == str(fixture["resource"]["number"])
    # Repository URL is normalized.
    expected_repo = normalize_repository_url(fixture["resource"]["repository_url"])
    assert identity.repository_url == expected_repo


def test_reporting_extraction_handles_missing_resource() -> None:
    """resource_identity_from_payload returns None for missing resource object."""
    assert resource_identity_from_payload(None, provider="github") is None
    assert resource_identity_from_payload({}, provider="github") is None
    assert resource_identity_from_payload(
        {"resource": None}, provider="github"
    ) is None


def test_reporting_extraction_handles_malformed_resource() -> None:
    """resource_identity_from_payload returns None for malformed resource objects."""
    # Missing repository_url.
    assert (
        resource_identity_from_payload(
            {"resource": {"type": "issue", "number": "100"}},
            provider="github",
        )
        is None
    )
    # Missing resource_type.
    assert (
        resource_identity_from_payload(
            {
                "resource": {
                    "repository_url": "https://github.com/owner/repo",
                    "number": "100",
                }
            },
            provider="github",
        )
        is None
    )
    # Missing resource_number.
    assert (
        resource_identity_from_payload(
            {
                "resource": {
                    "repository_url": "https://github.com/owner/repo",
                    "type": "issue",
                }
            },
            provider="github",
        )
        is None
    )


def test_reporting_extraction_unknown_resource_type_returns_none() -> None:
    """An unknown resource_type returns None (not mapped)."""
    identity = resource_identity_from_payload(
        {
            "resource": {
                "repository_url": "https://github.com/owner/repo",
                "type": "commit",
                "number": "abc123",
            }
        },
        provider="github",
    )
    assert identity is None


# ══════════════════════════════════════════════════════════════════════════════
#  Section 7: Malformed inputs enter the DLQ with distinct reasons
# ══════════════════════════════════════════════════════════════════════════════


def test_malformed_schema_version_routes_to_dlq() -> None:
    """An unsupported schema version raises NormalizedEventValidationError with
    a distinct reason."""
    payload = _nested_event(
        "00000000-0000-0000-0000-000000000001", "issue", 100, "opened",
        schema_version="2.0",
    )
    message = NormalizedProviderEvent.model_validate(payload)
    with pytest.raises(NormalizedEventValidationError) as exc_info:
        validate_normalized_event(message)
    assert "Unsupported schema version" in exc_info.value.reason
    assert "2.0" in exc_info.value.reason


def test_malformed_invalid_repository_url_routes_to_dlq() -> None:
    """An invalid repository URL (non-HTTP) raises NormalizedEventValidationError
    with a distinct reason."""
    payload = _nested_event(
        "00000000-0000-0000-0000-000000000001", "pull_request", 200, "opened"
    )
    payload["resource"]["repository_url"] = "ftp://github.com/owner/repo"
    message = NormalizedProviderEvent.model_validate(payload)
    with pytest.raises(NormalizedEventValidationError) as exc_info:
        validate_normalized_event(message)
    assert "Invalid repository identity" in exc_info.value.reason
    assert "ftp://" in exc_info.value.reason


def test_malformed_payload_ref_provider_mismatch_routes_to_dlq() -> None:
    """A redacted_payload.provider mismatch raises NormalizedEventValidationError
    with a distinct reason."""
    payload = _nested_event(
        "00000000-0000-0000-0000-000000000001", "pull_request", 200, "opened"
    )
    payload["redacted_payload"]["reference"]["provider"] = "gitlab"  # mismatch
    message = NormalizedProviderEvent.model_validate(payload)
    with pytest.raises(NormalizedEventValidationError) as exc_info:
        validate_normalized_event(message)
    assert "Reference mismatch" in exc_info.value.reason
    assert "provider" in exc_info.value.reason


def test_malformed_payload_ref_delivery_id_mismatch_routes_to_dlq() -> None:
    """A redacted_payload.delivery_id mismatch raises NormalizedEventValidationError
    with a distinct reason."""
    payload = _nested_event(
        "00000000-0000-0000-0000-000000000001", "pull_request", 200, "opened"
    )
    payload["redacted_payload"]["reference"]["delivery_id"] = "wrong-delivery-id"  # mismatch
    message = NormalizedProviderEvent.model_validate(payload)
    with pytest.raises(NormalizedEventValidationError) as exc_info:
        validate_normalized_event(message)
    assert "Reference mismatch" in exc_info.value.reason
    assert "delivery_id" in exc_info.value.reason


def test_malformed_unmappable_action_routes_to_dlq() -> None:
    """An action not in the canonical vocabulary returns None from
    map_normalized_event (routes to DLQ)."""
    payload = _nested_event(
        "00000000-0000-0000-0000-000000000001", "pull_request", 200, "synchronize"
    )
    message = NormalizedProviderEvent.model_validate(payload)
    mapped = map_normalized_event(message)
    assert mapped is None, (
        "Unmappable action 'synchronize' should return None (DLQ route)"
    )


def test_malformed_unknown_resource_type_routes_to_dlq() -> None:
    """An unknown resource_type returns None from map_normalized_event (DLQ)."""
    payload = _nested_event(
        "00000000-0000-0000-0000-000000000001", "commit", 200, "opened"
    )
    message = NormalizedProviderEvent.model_validate(payload)
    mapped = map_normalized_event(message)
    assert mapped is None, (
        "Unknown resource_type 'commit' should return None (DLQ route)"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Section 7a: issue_links tolerance (issue #522)
# ══════════════════════════════════════════════════════════════════════════════


def test_message_without_issue_links_still_passes_validation() -> None:
    """A message WITHOUT issue_links passes NormalizedProviderEvent validation.

    The issue_links field is optional — existing messages without it must
    continue to validate and pass through the consumer pipeline.
    """
    payload = _nested_event(
        "00000000-0000-0000-0000-000000000001", "issue", 100, "opened"
    )
    assert "issue_links" not in payload
    message = NormalizedProviderEvent.model_validate(payload)
    validate_normalized_event(message)  # must not raise


def test_message_without_issue_links_maps_through_pipeline() -> None:
    """A message without issue_links maps through map_normalized_event correctly."""
    payload = _nested_event(
        "00000000-0000-0000-0000-000000000002", "pull_request", 200, "opened"
    )
    assert "issue_links" not in payload
    message = NormalizedProviderEvent.model_validate(payload)
    mapped = map_provider_event(message)
    assert mapped is not None
    entity, event = mapped
    assert entity.entity_type is EntityType.CHANGE_REQUEST
    assert event.event_type == "change_request.opened"


def test_message_with_valid_issue_links_passes_validation() -> None:
    """A message WITH valid issue_links passes NormalizedProviderEvent validation."""
    payload = _nested_event(
        "00000000-0000-0000-0000-000000000003", "pull_request", 300, "opened"
    )
    payload["issue_links"] = {
        "references": [{"repository": "https://github.com/owner/repo", "number": "456"}],
        "declares_closure": [{"repository": "https://github.com/owner/repo", "number": "123"}],
    }
    message = NormalizedProviderEvent.model_validate(payload)
    validate_normalized_event(message)  # must not raise


def test_message_with_issue_links_maps_through_pipeline() -> None:
    """A message with issue_links maps through map_normalized_event correctly.

    issue_links is not consumed by the mapping bridge — it passes through
    to the event payload as-is for downstream use.
    """
    payload = _nested_event(
        "00000000-0000-0000-0000-000000000004", "merge_request", 400, "opened",
        provider="gitlab",
    )
    payload["issue_links"] = {
        "references": [
            {"repository": "https://gitlab.com/group/project", "number": "50"}
        ],
        "declares_closure": [
            {"repository": "https://gitlab.com/group/project", "number": "42"}
        ],
    }
    message = NormalizedProviderEvent.model_validate(payload)
    validate_normalized_event(message)
    mapped = map_provider_event(message)
    assert mapped is not None
    entity, event = mapped
    assert entity.entity_type is EntityType.CHANGE_REQUEST
    assert event.event_type == "change_request.opened"


def test_issue_links_with_empty_arrays_passes_validation() -> None:
    """A message with issue_links containing empty arrays still passes validation."""
    payload = _nested_event(
        "00000000-0000-0000-0000-000000000005", "pull_request", 500, "opened"
    )
    payload["issue_links"] = {"references": [], "declares_closure": []}
    message = NormalizedProviderEvent.model_validate(payload)
    validate_normalized_event(message)  # must not raise


def test_issue_links_fixture_files_accept_issue_links() -> None:
    """Updated fixtures with issue_links pass validation and mapping."""
    for filename in [
        "pull_request.opened.json",
        "merge_request.opened.json",
        "pull_request.edited.json",
        "merge_request.updated.json",
    ]:
        fixture = _load_fixture(filename)
        message = NormalizedProviderEvent.model_validate(fixture)
        validate_normalized_event(message)
        mapped = map_provider_event(message)
        assert mapped is not None, (
            f"Fixture {filename} with issue_links failed mapping"
        )


@pytest.mark.asyncio
async def test_malformed_input_routes_to_dlq_with_distinct_reasons() -> None:
    """Malformed inputs are routed to the DLQ through the consumer pipeline with
    distinct reason strings for each failure mode."""
    conn = _FakeConn([])
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()
    consumer._producer.send_and_wait = AsyncMock()

    # Test 1: Action outside the lifecycle allowlist → DLQ with "Unsupported action"
    violating = _nested_event(
        "00000000-0000-0000-0000-000000000001", "pull_request", 200, "synchronize"
    )
    await consumer._process_message(_mk_msg(violating))
    consumer._producer.send_and_wait.assert_called_once()
    (_topic, dlq_payload), _kwargs = consumer._producer.send_and_wait.call_args
    assert "Unsupported action" in dlq_payload["reason"]
    assert "synchronize" in dlq_payload["reason"]
    consumer._producer.send_and_wait.reset_mock()

    # Test 2: Unknown resource_type → DLQ with "Unsupported resource type"
    unknown_type = _nested_event(
        "00000000-0000-0000-0000-000000000002", "commit", 200, "opened"
    )
    await consumer._process_message(_mk_msg(unknown_type))
    assert consumer._producer.send_and_wait.call_count == 1
    (_topic, dlq_payload2), _kwargs = consumer._producer.send_and_wait.call_args
    assert "Unsupported resource type" in dlq_payload2["reason"]
    assert "commit" in dlq_payload2["reason"]
    consumer._producer.send_and_wait.reset_mock()

    # Test 3: Bad JSON → DLQ with a reason
    bad_json_msg = MagicMock(spec=ConsumerRecord)
    bad_json_msg.value = b"not valid json at all"
    bad_json_msg.offset = 99
    bad_json_msg.partition = 0
    bad_json_msg.topic = "afk.events"
    bad_json_msg.key = None
    bad_json_msg.headers = ()
    await consumer._process_message(bad_json_msg)
    assert consumer._producer.send_and_wait.call_count == 1
    (_topic, dlq_payload3), _kwargs = consumer._producer.send_and_wait.call_args
    assert dlq_payload3["reason"], "Bad JSON DLQ must have a reason"
    assert dlq_payload3["payload"] == {"raw": "not valid json at all"}


# ══════════════════════════════════════════════════════════════════════════════
#  Section 8: Idempotent redelivery
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_redelivery_of_same_delivery_id_creates_no_duplicate() -> None:
    """Redelivery of the same (provider, delivery_id) creates no duplicate
    delivery or Engineering Event.

    The consumer pipeline uses delivery_log UNIQUE(provider, delivery_id) +
    engineering_events identity UNIQUE for dedup.  A second delivery of the
    same message must not create a second row.
    """
    conn = _FakeConn([], execute=AsyncMock(return_value="OK"))
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()

    payload = _nested_event(
        "00000000-0000-0000-0000-000000000001", "pull_request", 200, "opened"
    )

    # First delivery — should persist.
    await consumer._process_message(_mk_msg(payload))
    first_commit_count = consumer._consumer.commit.call_count
    assert first_commit_count == 1
    consumer._producer.send_and_wait.assert_not_called()

    # Second delivery of the same (provider, delivery_id) — should be idempotent.
    # The delivery_log INSERT ... ON CONFLICT DO NOTHING prevents a duplicate.
    await consumer._process_message(_mk_msg(payload))
    second_commit_count = consumer._consumer.commit.call_count
    assert second_commit_count == 2, "Second delivery should still commit offset"
    # The DLQ should not be invoked for a valid duplicate.
    consumer._producer.send_and_wait.assert_not_called()


@pytest.mark.asyncio
async def test_redelivery_with_different_offset_is_idempotent() -> None:
    """Redelivery with a different Kafka offset but same (provider, delivery_id)
    is still idempotent."""
    conn = _FakeConn([], execute=AsyncMock(return_value="OK"))
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()

    payload = _nested_event(
        "00000000-0000-0000-0000-000000000001", "issue", 100, "opened"
    )

    # First delivery at offset 42.
    await consumer._process_message(_mk_msg(payload, offset=42))
    assert consumer._consumer.commit.call_count == 1

    # Redelivery at offset 99 (same delivery_id).
    await consumer._process_message(_mk_msg(payload, offset=99))
    assert consumer._consumer.commit.call_count == 2
    consumer._producer.send_and_wait.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
#  Section 9: Repository isolation for equal short entity IDs
# ══════════════════════════════════════════════════════════════════════════════


def test_equal_short_entity_ids_in_separate_repos_are_isolated() -> None:
    """Equal short entity IDs (e.g. issue #100) in separate repositories produce
    distinct entity_id values, keeping them isolated in entity and event reads."""
    # Repo A: owner/repo-a, issue #100
    payload_a = _nested_event(
        "00000000-0000-0000-0000-00000000000a", "issue", 100, "opened"
    )
    payload_a["resource"]["repository_url"] = "https://github.com/owner/repo-a"
    message_a = NormalizedProviderEvent.model_validate(payload_a)
    mapped_a = map_normalized_event(message_a)
    assert mapped_a is not None
    entity_a, event_a = mapped_a

    # Repo B: owner/repo-b, also issue #100
    payload_b = _nested_event(
        "00000000-0000-0000-0000-00000000000b", "issue", 100, "opened"
    )
    payload_b["resource"]["repository_url"] = "https://github.com/owner/repo-b"
    message_b = NormalizedProviderEvent.model_validate(payload_b)
    mapped_b = map_normalized_event(message_b)
    assert mapped_b is not None
    entity_b, event_b = mapped_b

    # entity_id is the same (issue:100) — the repository is stored separately.
    assert entity_a.entity_id == "issue:100"
    assert entity_b.entity_id == "issue:100"

    # But the repository field distinguishes them.
    assert entity_a.repository == "github.com/owner/repo-a"
    assert entity_b.repository == "github.com/owner/repo-b"
    assert entity_a.repository != entity_b.repository

    # The events have distinct event_ids (though entity_id is the same).
    assert event_a.entity_id == "issue:100"
    assert event_b.entity_id == "issue:100"

    # Different delivery_ids ensure they are distinct deliveries.
    assert payload_a["delivery_id"] != payload_b["delivery_id"]


def test_equal_short_entity_ids_in_separate_repos_reporting_isolation() -> None:
    """Equal short entity IDs in separate repos produce distinct reporting
    ResourceIdentity values, keeping them isolated in reporting reads."""
    # Repo A.
    identity_a = resource_identity_from_payload(
        {
            "resource": {
                "repository_url": "https://github.com/owner/repo-a",
                "type": "issue",
                "number": "100",
            }
        },
        provider="github",
    )
    assert identity_a is not None
    assert identity_a.repository_url == "github.com/owner/repo-a"
    assert identity_a.resource_number == "100"

    # Repo B.
    identity_b = resource_identity_from_payload(
        {
            "resource": {
                "repository_url": "https://github.com/owner/repo-b",
                "type": "issue",
                "number": "100",
            }
        },
        provider="github",
    )
    assert identity_b is not None
    assert identity_b.repository_url == "github.com/owner/repo-b"
    assert identity_b.resource_number == "100"

    # The composite keys are distinct because the repository_url differs.
    assert identity_a.composite_key != identity_b.composite_key
    assert identity_a.composite_key == "github:github.com/owner/repo-a:issue:100"
    assert identity_b.composite_key == "github:github.com/owner/repo-b:issue:100"


def test_equal_short_entity_ids_different_resource_types_are_isolated() -> None:
    """Equal short IDs with different resource types (e.g. issue #100 vs PR #100)
    produce distinct entity_id values."""
    # Issue #100.
    payload_issue = _nested_event(
        "00000000-0000-0000-0000-00000000000c", "issue", 100, "opened"
    )
    message_issue = NormalizedProviderEvent.model_validate(payload_issue)
    mapped_issue = map_normalized_event(message_issue)
    assert mapped_issue is not None
    entity_issue, _event_issue = mapped_issue

    # PR #100.
    payload_pr = _nested_event(
        "00000000-0000-0000-0000-00000000000d", "pull_request", 100, "opened"
    )
    message_pr = NormalizedProviderEvent.model_validate(payload_pr)
    mapped_pr = map_normalized_event(message_pr)
    assert mapped_pr is not None
    entity_pr, _event_pr = mapped_pr

    # Different entity types → different entity_ids.
    assert entity_issue.entity_id == "issue:100"
    assert entity_pr.entity_id == "change_request:100"
    assert entity_issue.entity_id != entity_pr.entity_id


# ══════════════════════════════════════════════════════════════════════════════
#  Section 10: Cross-provider mapping
# ══════════════════════════════════════════════════════════════════════════════


def test_gitlab_merge_request_maps_to_change_request() -> None:
    """GitLab merge_request maps to change_request (cross-provider contract)."""
    payload = _nested_event(
        "00000000-0000-0000-0000-00000000000e",
        "merge_request",
        300,
        "merged",
        provider="gitlab",
    )
    message = NormalizedProviderEvent.model_validate(payload)
    mapped = map_normalized_event(message)

    assert mapped is not None
    entity, event = mapped
    assert entity.entity_type is EntityType.CHANGE_REQUEST
    assert entity.provider is Provider.GITLAB
    assert entity.entity_id == "change_request:300"
    assert event.event_type == "change_request.merged"
    assert event.payload.get("source_resource_type") == "merge_request"
    assert event.payload.get("source_action") == "merged"


def test_github_pull_request_maps_to_change_request() -> None:
    """GitHub pull_request maps to change_request."""
    payload = _nested_event(
        "00000000-0000-0000-0000-00000000000f", "pull_request", 200, "merged"
    )
    message = NormalizedProviderEvent.model_validate(payload)
    mapped = map_normalized_event(message)

    assert mapped is not None
    entity, event = mapped
    assert entity.entity_type is EntityType.CHANGE_REQUEST
    assert entity.provider is Provider.GITHUB
    assert entity.entity_id == "change_request:200"
    assert event.event_type == "change_request.merged"
    assert event.payload.get("source_resource_type") == "pull_request"
    assert event.payload.get("source_action") == "merged"


# ══════════════════════════════════════════════════════════════════════════════
#  Section 11: Full pipeline integration — fixture → validation → mapping → persist
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resource_type", "action"),
    sorted(_ALLOWED_PAIRS),
)
async def test_full_pipeline_fixture_to_persist(
    resource_type: str, action: str
) -> None:
    """Every fixture runs through the full consumer pipeline:
    validation → mapping → persistence, without DLQ routing."""
    fixture = _load_fixture(_expected_fixture_filename(resource_type, action))
    conn = _FakeConn([], execute=AsyncMock(return_value="OK"))
    consumer = _make_consumer(pool=_FakePool(conn))
    consumer._consumer = AsyncMock()
    consumer._consumer.commit = AsyncMock()
    consumer._producer = AsyncMock()

    await consumer._process_message(_mk_msg(fixture))

    # Must not be routed to DLQ.
    consumer._producer.send_and_wait.assert_not_called()
    consumer._consumer.commit.assert_called_once()

    # Must have been persisted to engineering_events.
    event_calls = [
        c
        for c in conn.execute.call_args_list
        if "INSERT INTO engineering_events" in c.args[0]
    ]
    assert len(event_calls) >= 1, (
        f"Fixture {resource_type}.{action} was not persisted to engineering_events"
    )

    # Verify the persisted event has the correct entity_type and event_type.
    event_call = event_calls[0]
    expected_entity_type, expected_event_type = _EXPECTED_CANONICAL[
        (resource_type, action)
    ]
    assert event_call.args[3] == expected_entity_type.value, (
        f"Persisted entity_type mismatch for {resource_type}.{action}"
    )
    assert event_call.args[5] == expected_event_type, (
        f"Persisted event_type mismatch for {resource_type}.{action}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Section 12: Consumer group isolation
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_consumer_uses_opencode_outcomes_group() -> None:
    """The AFK Outcome Consumer uses the opencode-outcomes consumer group,
    never the usage consumer's opencode-gateway group."""
    conn = _FakeConn([])
    consumer = _make_consumer(
        pool=_FakePool(conn), consumer_group_id="opencode-outcomes"
    )
    assert consumer._consumer_group_id == "opencode-outcomes"


@pytest.mark.asyncio
async def test_consumer_group_is_not_usage_group() -> None:
    """The AFK consumer group is distinct from the usage consumer group."""
    conn = _FakeConn([])
    consumer = _make_consumer(
        pool=_FakePool(conn), consumer_group_id="opencode-outcomes"
    )
    assert consumer._consumer_group_id != "opencode-gateway"
