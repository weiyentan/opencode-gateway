"""Tests for ``afk_outcomes.models.build_observation_key`` (issue #527).

The observation key is a deterministic SHA-256 over the canonical form of an
engineering event fact's six identity fields.  These tests pin the
occurred_at normalisation contract: the same instant must derive the
identical key whether it arrives naive, as UTC, or with any other UTC offset
— and distinct instants must derive distinct keys.
"""

from datetime import datetime, timedelta, timezone

from afk_outcomes.models import build_observation_key

PROVIDER = "github"
REPOSITORY = "github.com/owner/repo"
ENTITY_TYPE = "change_request"
EXTERNAL_ID = "442"
EVENT_TYPE = "change_request.merged"

PLUS_05_30 = timezone(timedelta(hours=5, minutes=30))


def _key(occurred_at: datetime) -> str:
    return build_observation_key(
        provider=PROVIDER,
        repository=REPOSITORY,
        entity_type=ENTITY_TYPE,
        external_id=EXTERNAL_ID,
        event_type=EVENT_TYPE,
        occurred_at=occurred_at,
    )


def test_naive_occurred_at_matches_aware_utc() -> None:
    """A naive timestamp is interpreted as UTC: the key matches aware-UTC."""
    naive = datetime(2026, 8, 1, 10, 30, 0)
    aware_utc = datetime(2026, 8, 1, 10, 30, 0, tzinfo=timezone.utc)
    assert _key(naive) == _key(aware_utc)


def test_naive_occurred_at_matches_same_instant_with_offset() -> None:
    """The same instant expressed with a non-UTC offset derives the same key."""
    naive = datetime(2026, 8, 1, 10, 30, 0)
    same_instant = datetime(2026, 8, 1, 16, 0, 0, tzinfo=PLUS_05_30)
    assert _key(naive) == _key(same_instant)


def test_distinct_instants_derive_distinct_keys() -> None:
    """Different occurrence times must never collide on the same key."""
    first = _key(datetime(2026, 8, 1, 10, 30, 0, tzinfo=timezone.utc))
    second = _key(datetime(2026, 8, 1, 10, 30, 1, tzinfo=timezone.utc))
    assert first != second
