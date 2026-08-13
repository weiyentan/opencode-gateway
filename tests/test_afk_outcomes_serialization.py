"""Serialization round-trip, determinism, and canonical-form tests.

Verifies the acceptance criteria: stable canonical JSON with sorted keys
and deterministic output, and lossless round-trip through ``dumps_canonical``
/ ``loads_canonical``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from afk_outcomes import (
    AFKRun,
    Correlation,
    CorrelationEvidence,
    EngineeringEntity,
    EngineeringEvent,
    EngineeringOutcome,
    EngineeringOutcomeStatus,
    EntityType,
    Provider,
    RunEntityLink,
    RunSessionLink,
    RunStatus,
    dumps_canonical,
    loads_canonical,
    make_ulid,
)

UTC = timezone.utc  # noqa: UP017 - datetime.UTC is 3.11+


def build_run() -> AFKRun:
    """A representative AFK run exercising every model type."""
    started = datetime(2026, 8, 13, 8, 0, 0, tzinfo=UTC)
    finished = datetime(2026, 8, 13, 10, 10, 29, tzinfo=UTC)
    merged = datetime(2026, 8, 13, 10, 10, 29, 123456, tzinfo=UTC)

    return AFKRun(
        afk_run_id="01J0000000000000000000000001",
        provider=Provider.GITHUB,
        status=RunStatus.COMPLETED,
        title="Develop-Loop: Consolidated run — Implemented issues #437, #438, #439, #440",
        started_at=started,
        finished_at=finished,
        entities=[
            EngineeringEntity(
                entity_id="issue:437",
                entity_type=EntityType.ISSUE,
                provider=Provider.GITHUB,
                repository="weiyentan/opencode-gateway",
                number=437,
                title="Add agent group-by dimension to usage aggregates",
                state="closed",
                author="wyautomation",
                url="https://github.com/weiyentan/opencode-gateway/issues/437",
                created_at=started,
            ),
            EngineeringEntity(
                entity_id="change_request:442",
                entity_type=EntityType.CHANGE_REQUEST,
                provider=Provider.GITHUB,
                repository="weiyentan/opencode-gateway",
                number=442,
                title="Develop-Loop: Consolidated run — Implemented issues #437, #438, #439, #440",
                state="merged",
                author="wyautomation",
            ),
        ],
        events=[
            EngineeringEvent(
                event_id="commit:abc",
                event_type="committed",
                provider=Provider.GITHUB,
                entity_id="change_request:442",
                occurred_at=started,
                actor="wyautomation",
                payload={"sha": "337c0116775c51abf03d90e73a9afdcee0aef01a"},
            ),
        ],
        correlations=[
            Correlation(
                correlation_id="corr-1",
                afk_run_id="01J0000000000000000000000001",
                entity_id="issue:437",
                correlation_confidence=1.0,
                method="issue_resolved",
                evidence=[
                    CorrelationEvidence(
                        kind="commit_message_reference",
                        source_entity_id="commit:abc",
                        detail="resolves #437",
                        weight=1.0,
                    ),
                ],
            ),
        ],
        outcome=EngineeringOutcome(
            status=EngineeringOutcomeStatus.MERGED,
            change_request_ids=["change_request:442"],
            resolved_issue_ids=["issue:437"],
            merge_event_id="merge_event:442",
            merged_at=merged,
        ),
        entity_links=[
            RunEntityLink(
                afk_run_id="01J0000000000000000000000001",
                entity_id="issue:437",
                role="resolved",
                correlation_confidence=1.0,
            ),
        ],
        session_links=[
            RunSessionLink(
                afk_run_id="01J0000000000000000000000001",
                session_id="00000000-0000-0000-0000-000000000001",
                external_session_id="ses_01J000000000000000000000001",
                started_at=started,
                finished_at=finished,
            ),
        ],
    )


def test_deterministic_serialization() -> None:
    run = build_run()
    assert dumps_canonical(run) == dumps_canonical(run)


def test_sorted_keys_at_every_level() -> None:
    run = build_run()
    serialized = dumps_canonical(run)

    def assert_sorted(obj: object) -> None:
        if isinstance(obj, dict):
            keys = list(obj.keys())
            assert keys == sorted(keys), f"keys not sorted: {keys}"
            for value in obj.values():
                assert_sorted(value)
        elif isinstance(obj, list):
            for item in obj:
                assert_sorted(item)

    assert_sorted(json.loads(serialized))


def test_no_whitespace() -> None:
    """Structural whitespace is eliminated (compact separators).

    Re-serialising the parsed payload with compact separators must yield
    byte-for-byte the same string — any stray structural whitespace would
    break that equality, while whitespace inside string values is fine.
    """
    serialized = dumps_canonical(build_run())
    assert "\n" not in serialized
    assert "\t" not in serialized
    assert serialized == json.dumps(
        json.loads(serialized), sort_keys=True, separators=(",", ":")
    )


def test_versioned_envelope() -> None:
    envelope = json.loads(dumps_canonical(build_run()))
    assert envelope["schema_version"] == 1
    assert "data" in envelope


def test_datetimes_normalised_to_utc_iso_z() -> None:
    run = build_run()
    # A non-UTC aware datetime must still serialize to the UTC 'Z' form:
    # 20:00 +12:00 normalises to 08:00Z.
    run = run.model_copy(
        update={
            "started_at": datetime(2026, 8, 13, 20, 0, 0, tzinfo=timezone(timedelta(hours=12))),
        }
    )
    plain = json.loads(dumps_canonical(run))
    assert plain["data"]["started_at"] == "2026-08-13T08:00:00Z"
    assert plain["data"]["outcome"]["merged_at"] == "2026-08-13T10:10:29.123456Z"


def test_round_trip_is_lossless() -> None:
    run = build_run()
    reloaded = loads_canonical(dumps_canonical(run))
    assert reloaded == run


def test_round_trip_is_idempotent() -> None:
    run = build_run()
    once = dumps_canonical(run)
    twice = dumps_canonical(loads_canonical(once))
    assert once == twice


def test_enums_serialize_as_strings() -> None:
    plain = json.loads(dumps_canonical(build_run()))
    assert plain["data"]["provider"] == "github"
    assert plain["data"]["status"] == "completed"
    assert plain["data"]["outcome"]["status"] == "merged"


def test_make_ulid_is_stable_and_26_chars() -> None:
    ulid = make_ulid(timestamp_ms=0, randomness=0)
    assert ulid == "00000000000000000000000000"
    assert len(ulid) == 26
    ulid2 = make_ulid(timestamp_ms=1_750_000_000_000, randomness=0)
    assert len(ulid2) == 26
    assert make_ulid(1, 0) != make_ulid(1, 1)
